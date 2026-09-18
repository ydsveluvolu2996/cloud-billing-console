"""Narrow, add-only IAM adapter for the native onboarding coordinator.

The single DynamoDB item is authoritative: IAM reads are eventually consistent
and cannot safely serve as the source of a read/merge/write role allowlist.
"""

import json
import logging
import os
import re
import time


POLICY_NAME = "DashboardOnboardedCustomerRoles"
POLICY_SID = "DashboardOnboardedCustomerRoles"
MAX_POLICY_CHARACTERS = 4096
ROLE_ARN = re.compile(
    r"arn:aws:iam::[0-9]{12}:role/BillingConsole/"
    r"(?:[A-Za-z0-9+=,.@_-]+/)*[A-Za-z0-9+=,.@_-]{1,64}\Z"
)
LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


class OnboardingBrokerError(RuntimeError):
    """Safe, actionable error returned through Lambda's FunctionError result."""


def validate_role_arn(value):
    if not isinstance(value, str) or not ROLE_ARN.fullmatch(value) or len(value) > 610:
        raise OnboardingBrokerError(
            "Use an exact AWS role ARN under the BillingConsole/ path; "
            "wildcards, other partitions, and non-role principals are not allowed."
        )
    # IAM limits the entire role path (including both slashes) to 512 characters.
    path = "/" + value.split(":role/", 1)[1].rsplit("/", 1)[0] + "/"
    if len(path) > 512:
        raise OnboardingBrokerError("The customer IAM role path exceeds 512 characters.")
    return value


def policy_document(roles):
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": POLICY_SID,
            "Effect": "Allow",
            "Action": "sts:AssumeRole",
            "Resource": sorted(roles),
        }],
    }


def serialize_policy(roles):
    encoded = json.dumps(policy_document(roles), separators=(",", ":"), sort_keys=True)
    if len(encoded) > MAX_POLICY_CHARACTERS:
        raise OnboardingBrokerError(
            "The dashboard onboarding role policy has reached its safe capacity. "
            "A hosting administrator must review IAM policy capacity before retrying; "
            "existing accounts remain configured."
        )
    return encoded


def validated_policy_roles(document):
    """Accept only this function's canonical policy, never preserve broad grants."""
    try:
        if not isinstance(document, dict) or set(document) != {"Version", "Statement"}:
            raise ValueError
        statements = document["Statement"]
        if document["Version"] != "2012-10-17" or not isinstance(statements, list) or len(statements) != 1:
            raise ValueError
        statement = statements[0]
        if not isinstance(statement, dict) or set(statement) != {"Sid", "Effect", "Action", "Resource"}:
            raise ValueError
        if statement["Sid"] != POLICY_SID or statement["Effect"] != "Allow" or statement["Action"] != "sts:AssumeRole":
            raise ValueError
        roles = statement["Resource"]
        if not isinstance(roles, list) or not roles or len(roles) != len(set(roles)):
            raise ValueError
        for role in roles:
            validate_role_arn(role)
        serialize_policy(roles)
        return set(roles)
    except (ValueError, TypeError, KeyError, OnboardingBrokerError) as exc:
        raise OnboardingBrokerError(
            "The dedicated onboarding IAM policy has unexpected contents. "
            "A hosting administrator must review it; no policy was changed."
        ) from exc


def error_code(exc):
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "")


def read_policy(iam, collector):
    try:
        response = iam.get_role_policy(RoleName=collector, PolicyName=POLICY_NAME)
    except Exception as exc:
        if error_code(exc) == "NoSuchEntity":
            return set()
        raise OnboardingBrokerError(
            "Cannot read the hosting onboarding policy. Check the broker's IAM access and retry."
        ) from exc
    return validated_policy_roles(response.get("PolicyDocument"))


def read_state(dynamodb, table, collector):
    try:
        item = dynamodb.get_item(
            TableName=table, Key={"CollectorRoleName": {"S": collector}}, ConsistentRead=True
        ).get("Item")
    except Exception as exc:
        raise OnboardingBrokerError("Cannot read onboarding state. Retry when the hosting service is available.") from exc
    if not item:
        return set(), None
    try:
        if set(item) != {"CollectorRoleName", "Roles", "Version"} or item["CollectorRoleName"] != {"S": collector}:
            raise ValueError
        if set(item["Roles"]) != {"SS"} or set(item["Version"]) != {"N"}:
            raise ValueError
        raw_roles = item["Roles"]["SS"]
        if not isinstance(raw_roles, list) or not raw_roles or len(raw_roles) != len(set(raw_roles)):
            raise ValueError
        version = int(item["Version"]["N"])
        if str(version) != item["Version"]["N"] or version < 1:
            raise ValueError
        roles = {validate_role_arn(role) for role in raw_roles}
        serialize_policy(roles)
        return roles, version
    except (ValueError, TypeError, KeyError, OnboardingBrokerError) as exc:
        raise OnboardingBrokerError("Onboarding state is invalid. A hosting administrator must review it.") from exc


def persist_state(dynamodb, table, collector, roles, previous_version):
    values = {":roles": {"SS": sorted(roles)}, ":next": {"N": str((previous_version or 0) + 1)}}
    names = {"#roles": "Roles", "#version": "Version"}
    if previous_version is None:
        condition = "attribute_not_exists(#key)"
        names["#key"] = "CollectorRoleName"
    else:
        condition = "#version = :previous"
        values[":previous"] = {"N": str(previous_version)}
    try:
        dynamodb.update_item(
            TableName=table,
            Key={"CollectorRoleName": {"S": collector}},
            UpdateExpression="SET #roles = :roles, #version = :next",
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except Exception as exc:
        if error_code(exc) == "ConditionalCheckFailedException":
            raise OnboardingBrokerError("Onboarding state changed concurrently. Retry this account.") from exc
        raise OnboardingBrokerError("Cannot save onboarding state. Retry when the hosting service is available.") from exc


def provision(event, iam, dynamodb, collector, table, sleeper=time.sleep):
    if not isinstance(event, dict) or set(event) != {"role_arn"}:
        raise OnboardingBrokerError("The request must contain only role_arn.")
    role = validate_role_arn(event["role_arn"])
    if collector != "CloudBillingCollector":
        raise OnboardingBrokerError("The broker target role configuration is invalid.")

    existing = read_policy(iam, collector)
    desired, version = read_state(dynamodb, table, collector)
    if not existing.issubset(desired):
        raise OnboardingBrokerError(
            "The dedicated IAM policy contains roles absent from onboarding state. "
            "A hosting administrator must reconcile it; no policy was changed."
        )
    updated = desired | {role}
    encoded = serialize_policy(updated)  # Reject capacity failures before persisting.
    if updated != desired:
        persist_state(dynamodb, table, collector, updated, version)

    if existing != updated:
        try:
            iam.put_role_policy(RoleName=collector, PolicyName=POLICY_NAME, PolicyDocument=encoded)
        except Exception as exc:
            if error_code(exc) in {"LimitExceeded", "LimitExceededException"}:
                raise OnboardingBrokerError(
                    "The collector has reached AWS's aggregate inline-policy quota. "
                    "A hosting administrator must review policy capacity. The request is saved; "
                    "retry after capacity is restored."
                ) from exc
            raise OnboardingBrokerError(
                "The request is saved but IAM activation failed. Check hosting broker permissions and retry."
            ) from exc
        for attempt in range(7):
            if read_policy(iam, collector) == updated:
                break
            sleeper(min(0.25 * (2 ** attempt), 2))
        else:
            raise OnboardingBrokerError(
                "The request is saved but IAM has not confirmed the update yet. Retry shortly."
            )

    LOG.info("Customer role registered collector=%s role_arn=%s", collector, role)
    return {"role_arn": role, "status": "allowed"}


def handler(event, context):
    import boto3
    from botocore.config import Config

    config = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 2, "mode": "standard"})
    return provision(
        event,
        boto3.client("iam", config=config),
        boto3.client("dynamodb", config=config),
        os.environ["COLLECTOR_ROLE_NAME"],
        os.environ["STATE_TABLE_NAME"],
    )
