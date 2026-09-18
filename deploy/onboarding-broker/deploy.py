"""Render or deploy the broker after verifying its independently managed boundary."""

import argparse
import json
from pathlib import Path
import re


DIRECTORY = Path(__file__).resolve().parent
COLLECTOR_ROLE_NAME = "CloudBillingCollector"


def build_template():
    template = json.loads((DIRECTORY / "template.json").read_text())
    template["Resources"]["Broker"]["Properties"]["Code"] = {
        "ZipFile": (DIRECTORY / "broker.py").read_text()
    }
    return template


def verify_boundary(iam, account_id, boundary_arn):
    if not re.fullmatch(rf"arn:aws:iam::{account_id}:policy/[A-Za-z0-9+=,.@_/-]+", boundary_arn):
        raise ValueError("The reviewed collector boundary must belong to the hosting account.")
    role = iam.get_role(RoleName=COLLECTOR_ROLE_NAME)["Role"]
    if role.get("PermissionsBoundary", {}).get("PermissionsBoundaryArn") != boundary_arn:
        raise ValueError("Install the reviewed collector permissions boundary before deploying the broker.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    parser.add_argument("--region", required=True)
    parser.add_argument("--boundary-arn", required=True)
    parser.add_argument("--stack-name", default="cloud-billing-onboarding-broker")
    parser.add_argument("--function-name", default="CloudBillingOnboardingBroker")
    parser.add_argument("--output", type=Path, help="Save the fully rendered CloudFormation template.")
    parser.add_argument("--apply", action="store_true", help="Deploy after the boundary preflight succeeds.")
    args = parser.parse_args()
    template = json.dumps(build_template(), indent=2)
    if args.output:
        args.output.write_text(template + "\n")
    if not args.apply:
        print(template)
        return

    import boto3
    from botocore.exceptions import ClientError

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    verify_boundary(session.client("iam"), account, args.boundary_arn)
    cfn = session.client("cloudformation")
    parameters = [
        {"ParameterKey": "FunctionName", "ParameterValue": args.function_name},
        {"ParameterKey": "CollectorBoundaryArn", "ParameterValue": args.boundary_arn},
    ]
    common = {
        "StackName": args.stack_name,
        "TemplateBody": template,
        "Parameters": parameters,
        "Capabilities": ["CAPABILITY_IAM"],
    }
    try:
        cfn.describe_stacks(StackName=args.stack_name)
        exists = True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ValidationError" and "does not exist" in str(exc):
            exists = False
        else:
            raise
    if exists:
        try:
            cfn.update_stack(**common)
        except ClientError as exc:
            if "No updates are to be performed" not in str(exc):
                raise
        else:
            cfn.get_waiter("stack_update_complete").wait(StackName=args.stack_name)
    else:
        cfn.create_stack(**common, EnableTerminationProtection=True)
        cfn.get_waiter("stack_create_complete").wait(StackName=args.stack_name)
    outputs = cfn.describe_stacks(StackName=args.stack_name)["Stacks"][0].get("Outputs", [])
    print(json.dumps({item["OutputKey"]: item["OutputValue"] for item in outputs}, indent=2))


if __name__ == "__main__":
    main()
