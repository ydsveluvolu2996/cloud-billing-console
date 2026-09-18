import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock


DIRECTORY = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, DIRECTORY / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


broker = load("broker")
deploy = load("deploy")
ROLE_A = "arn:aws:iam::111111111111:role/BillingConsole/CostReadOnly"
ROLE_B = "arn:aws:iam::222222222222:role/BillingConsole/CostReadOnly"
COLLECTOR = "CloudBillingCollector"
TABLE = "test-onboarding-state"


class AwsError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class FakeIam:
    def __init__(self, roles=None):
        self.policy = broker.policy_document(roles) if roles else None
        self.puts = []
        self.gets = []
        self.failure = None
        self.stale = None

    def get_role_policy(self, **kwargs):
        self.gets.append(kwargs)
        document = self.stale if self.stale is not None else self.policy
        if document is None:
            raise AwsError("NoSuchEntity")
        return {"PolicyDocument": copy.deepcopy(document)}

    def put_role_policy(self, **kwargs):
        self.puts.append(kwargs)
        if self.failure:
            raise AwsError(self.failure)
        self.policy = json.loads(kwargs["PolicyDocument"])


class FakeDynamo:
    def __init__(self, roles=None, version=1):
        self.item = None
        if roles:
            self.item = {
                "CollectorRoleName": {"S": COLLECTOR},
                "Roles": {"SS": sorted(roles)},
                "Version": {"N": str(version)},
            }
        self.gets = []
        self.updates = []
        self.failure = None

    def get_item(self, **kwargs):
        self.gets.append(kwargs)
        return {"Item": copy.deepcopy(self.item)} if self.item else {}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        if self.failure:
            raise AwsError(self.failure)
        values = kwargs["ExpressionAttributeValues"]
        self.item = {
            "CollectorRoleName": {"S": COLLECTOR},
            "Roles": values[":roles"],
            "Version": values[":next"],
        }


class BrokerTests(unittest.TestCase):
    def run_broker(self, role=ROLE_A, iam=None, ddb=None, event=None):
        return broker.provision(
            event if event is not None else {"role_arn": role},
            iam if iam is not None else FakeIam(),
            ddb if ddb is not None else FakeDynamo(),
            COLLECTOR, TABLE, sleeper=lambda _: None,
        )

    def test_first_registration_writes_only_dedicated_sts_policy_and_returns_exact_role(self):
        iam, ddb = FakeIam(), FakeDynamo()
        self.assertEqual(self.run_broker(iam=iam, ddb=ddb), {"role_arn": ROLE_A, "status": "allowed"})
        self.assertEqual(iam.policy, broker.policy_document({ROLE_A}))
        self.assertEqual(len(iam.puts), 1)
        self.assertEqual(iam.puts[0]["PolicyName"], "DashboardOnboardedCustomerRoles")
        self.assertEqual(iam.puts[0]["RoleName"], COLLECTOR)
        self.assertTrue(ddb.gets[0]["ConsistentRead"])
        self.assertEqual(ddb.updates[0]["ConditionExpression"], "attribute_not_exists(#key)")

    def test_retry_is_idempotent(self):
        iam, ddb = FakeIam({ROLE_A}), FakeDynamo({ROLE_A})
        self.run_broker(iam=iam, ddb=ddb)
        self.assertEqual(iam.puts, [])
        self.assertEqual(ddb.updates, [])

    def test_new_role_merges_authoritative_state(self):
        iam, ddb = FakeIam({ROLE_A}), FakeDynamo({ROLE_A}, version=9)
        self.run_broker(ROLE_B, iam, ddb)
        self.assertEqual(iam.policy, broker.policy_document({ROLE_A, ROLE_B}))
        self.assertEqual(ddb.updates[0]["ConditionExpression"], "#version = :previous")
        self.assertEqual(ddb.updates[0]["ExpressionAttributeValues"][":previous"], {"N": "9"})
        self.assertEqual(ddb.item["Version"], {"N": "10"})

    def test_eventually_consistent_iam_read_cannot_drop_previous_roles(self):
        iam, ddb = FakeIam(), FakeDynamo({ROLE_A})
        self.run_broker(ROLE_B, iam, ddb)
        self.assertEqual(iam.policy, broker.policy_document({ROLE_A, ROLE_B}))

    def test_pending_saved_role_is_reapplied_on_retry(self):
        iam, ddb = FakeIam(), FakeDynamo({ROLE_A})
        self.run_broker(iam=iam, ddb=ddb)
        self.assertEqual(iam.policy, broker.policy_document({ROLE_A}))
        self.assertEqual(ddb.updates, [])

    def test_payload_cannot_select_policy_action_collector_or_extra_data(self):
        for event in [[], "role", {}, {"role_arn": ROLE_A, "policy_name": "AdministratorAccess"},
                      {"role_arn": ROLE_A, "collector": "Admin"}, {"role_arn": ROLE_A, "action": "delete"}]:
            with self.subTest(event=event):
                iam, ddb = FakeIam(), FakeDynamo()
                with self.assertRaises(broker.OnboardingBrokerError):
                    broker.provision(event, iam, ddb, COLLECTOR, TABLE)
                self.assertEqual(iam.gets, [])
                self.assertEqual(ddb.gets, [])

    def test_only_exact_commercial_billing_console_roles_are_accepted(self):
        bad_roles = [
            "*", ROLE_A + "*", ROLE_A + "\n", " " + ROLE_A, ROLE_A.replace("aws:", "aws-cn:"),
            ROLE_A.replace("111111111111", "*"), ROLE_A.replace("111111111111", "11111111111"),
            ROLE_A.replace("role/", "user/"), ROLE_A.replace("BillingConsole/", ""),
            ROLE_A.replace("CostReadOnly", ""), ROLE_A.replace("CostReadOnly", "a" * 65),
            ROLE_A.replace("CostReadOnly", "part/" * 110 + "Read"), ROLE_A.replace("CostReadOnly", "Read%2A"),
            ROLE_A.replace("CostReadOnly", "Read?"), ROLE_A.replace("CostReadOnly", "a\\b"),
            ROLE_A.replace("CostReadOnly", "a//b"), None, 17, {"arn": ROLE_A},
        ]
        for role in bad_roles:
            with self.subTest(role=role), self.assertRaises(broker.OnboardingBrokerError):
                self.run_broker(role)
        self.assertEqual(broker.validate_role_arn(ROLE_A.replace("CostReadOnly", "Team/Cost+Read_Only")),
                         ROLE_A.replace("CostReadOnly", "Team/Cost+Read_Only"))

    def test_environment_cannot_retarget_another_collector_role(self):
        iam, ddb = FakeIam(), FakeDynamo()
        with self.assertRaises(broker.OnboardingBrokerError):
            broker.provision({"role_arn": ROLE_A}, iam, ddb, "Admin", TABLE)
        self.assertEqual(iam.gets, [])

    def test_broad_or_noncanonical_existing_policy_fails_closed(self):
        baseline = broker.policy_document({ROLE_A})
        mutations = [
            lambda p: p["Statement"][0].update(Action="*"),
            lambda p: p["Statement"][0].update(Action=["sts:AssumeRole", "iam:*"]),
            lambda p: p["Statement"][0].update(Resource=["*"]),
            lambda p: p["Statement"][0].update(Resource=ROLE_A),
            lambda p: p["Statement"][0].update(Resource=[ROLE_A, ROLE_A]),
            lambda p: p["Statement"][0].update(Resource=[{"Ref": "Role"}]),
            lambda p: p["Statement"][0].update(Effect="Deny"),
            lambda p: p["Statement"][0].update(NotResource="arn:aws:iam::*:role/Admin"),
            lambda p: p["Statement"][0].update(Condition={"Bool": {"aws:SecureTransport": "true"}}),
            lambda p: p["Statement"].append(copy.deepcopy(p["Statement"][0])),
            lambda p: p.update(Version="2008-10-17"),
            lambda p: p.update(Id="extra"),
        ]
        for mutate in mutations:
            iam, ddb = FakeIam({ROLE_A}), FakeDynamo({ROLE_A})
            iam.policy = copy.deepcopy(baseline)
            mutate(iam.policy)
            with self.subTest(policy=iam.policy), self.assertRaises(broker.OnboardingBrokerError):
                self.run_broker(ROLE_B, iam, ddb)
            self.assertEqual(iam.puts, [])
            self.assertEqual(ddb.updates, [])

    def test_manual_policy_addition_absent_from_state_requires_review(self):
        iam, ddb = FakeIam({ROLE_B}), FakeDynamo({ROLE_A})
        with self.assertRaisesRegex(broker.OnboardingBrokerError, "reconcile"):
            self.run_broker(ROLE_B, iam, ddb)
        self.assertEqual(iam.puts, [])
        self.assertEqual(ddb.updates, [])

    def test_state_missing_with_existing_policy_is_not_silently_recreated(self):
        with self.assertRaisesRegex(broker.OnboardingBrokerError, "reconcile"):
            self.run_broker(iam=FakeIam({ROLE_A}), ddb=FakeDynamo())

    def test_corrupt_state_fails_closed(self):
        mutations = [
            lambda i: i.update(Roles={"SS": ["*"]}),
            lambda i: i.update(Roles={"SS": []}),
            lambda i: i.update(Roles={"S": ROLE_A}),
            lambda i: i.update(Version={"N": "0"}),
            lambda i: i.update(Version={"N": "1.1"}),
            lambda i: i.update(CollectorRoleName={"S": "Admin"}),
            lambda i: i.update(Extra={"S": "x"}),
        ]
        for mutate in mutations:
            ddb = FakeDynamo({ROLE_A})
            mutate(ddb.item)
            iam = FakeIam()
            with self.subTest(item=ddb.item), self.assertRaises(broker.OnboardingBrokerError):
                self.run_broker(iam=iam, ddb=ddb)
            self.assertEqual(ddb.updates, [])
            self.assertEqual(iam.puts, [])

    def test_policy_capacity_rejected_before_persisting_or_iam_write(self):
        roles = set()
        for number in range(1000):
            candidate = f"arn:aws:iam::{number:012}:role/BillingConsole/CostReadOnly"
            try:
                broker.serialize_policy(roles | {candidate})
            except broker.OnboardingBrokerError:
                break
            roles.add(candidate)
        iam, ddb = FakeIam(roles), FakeDynamo(roles)
        with self.assertRaisesRegex(broker.OnboardingBrokerError, "capacity"):
            self.run_broker(ROLE_A, iam, ddb)
        self.assertEqual(ddb.updates, [])
        self.assertEqual(iam.puts, [])

    def test_aggregate_iam_quota_failure_retains_request_for_retry(self):
        iam, ddb = FakeIam(), FakeDynamo()
        iam.failure = "LimitExceeded"
        with self.assertRaisesRegex(broker.OnboardingBrokerError, "aggregate inline-policy quota"):
            self.run_broker(iam=iam, ddb=ddb)
        self.assertEqual(ddb.item["Roles"], {"SS": [ROLE_A]})
        self.assertIsNone(iam.policy)
        iam.failure = None
        self.assertEqual(self.run_broker(iam=iam, ddb=ddb)["status"], "allowed")

    def test_iam_access_denied_does_not_report_success(self):
        iam, ddb = FakeIam(), FakeDynamo()
        iam.failure = "AccessDenied"
        with self.assertRaisesRegex(broker.OnboardingBrokerError, "activation failed"):
            self.run_broker(iam=iam, ddb=ddb)
        self.assertIsNotNone(ddb.item)

    def test_conditional_write_failure_does_not_write_iam(self):
        iam, ddb = FakeIam(), FakeDynamo()
        ddb.failure = "ConditionalCheckFailedException"
        with self.assertRaisesRegex(broker.OnboardingBrokerError, "concurrently"):
            self.run_broker(iam=iam, ddb=ddb)
        self.assertEqual(iam.puts, [])

    def test_confirmation_timeout_does_not_report_success(self):
        iam, ddb = FakeIam({ROLE_A}), FakeDynamo({ROLE_A})
        iam.stale = broker.policy_document({ROLE_A})
        with self.assertRaisesRegex(broker.OnboardingBrokerError, "not confirmed"):
            self.run_broker(ROLE_B, iam, ddb)
        self.assertEqual(ddb.item["Roles"], {"SS": sorted({ROLE_A, ROLE_B})})


class InfrastructureTests(unittest.TestCase):
    def setUp(self):
        self.template = deploy.build_template()
        self.resources = self.template["Resources"]

    def test_private_function_has_no_public_endpoint_or_invocation_policy(self):
        forbidden = {"AWS::Lambda::Permission", "AWS::Lambda::Url", "AWS::ApiGateway::RestApi", "AWS::ApiGatewayV2::Api"}
        self.assertFalse(any(resource["Type"] in forbidden for resource in self.resources.values()))
        self.assertEqual(self.resources["Broker"]["Properties"]["ReservedConcurrentExecutions"], 1)

    def test_execution_permissions_only_touch_exact_collector_table_and_logs(self):
        statements = self.resources["ExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        self.assertEqual(statements[0]["Action"], ["iam:GetRolePolicy", "iam:PutRolePolicy"])
        self.assertEqual(statements[0]["Resource"], {"Fn::Sub": "arn:aws:iam::${AWS::AccountId}:role/CloudBillingCollector"})
        self.assertEqual(statements[1]["Action"], ["dynamodb:GetItem", "dynamodb:UpdateItem"])
        self.assertEqual(statements[1]["Resource"], {"Fn::GetAtt": ["StateTable", "Arn"]})
        self.assertEqual(statements[1]["Condition"], {"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": [COLLECTOR]}})
        self.assertEqual(statements[2]["Action"], ["logs:CreateLogStream", "logs:PutLogEvents"])
        self.assertEqual(len(statements), 3)

    def test_collector_only_receives_exact_lambda_invocation_permission(self):
        permission = self.resources["CollectorInvocation"]["Properties"]
        self.assertEqual(permission["Roles"], [COLLECTOR])
        self.assertEqual(permission["PolicyDocument"]["Statement"], [{
            "Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": {"Fn::GetAtt": ["Broker", "Arn"]}
        }])

    def test_durable_state_is_retained_encrypted_and_recoverable(self):
        table = self.resources["StateTable"]
        self.assertEqual(table["DeletionPolicy"], "Retain")
        self.assertEqual(table["UpdateReplacePolicy"], "Retain")
        self.assertEqual(table["Properties"]["BillingMode"], "PAY_PER_REQUEST")
        self.assertEqual(table["Properties"]["SSESpecification"], {"SSEEnabled": True})
        self.assertEqual(table["Properties"]["PointInTimeRecoverySpecification"], {"PointInTimeRecoveryEnabled": True})

    def test_rendered_template_includes_reviewed_source_not_placeholder(self):
        self.assertEqual(self.resources["Broker"]["Properties"]["Code"]["ZipFile"], (DIRECTORY / "broker.py").read_text())
        self.assertLess(len(json.dumps(self.template)), 51200)

    def test_deploy_refuses_missing_mismatched_or_other_account_boundary(self):
        boundary = "arn:aws:iam::111111111111:policy/ReviewedCollectorBoundary"
        for actual in [None, "arn:aws:iam::111111111111:policy/OtherBoundary"]:
            iam = Mock()
            iam.get_role.return_value = {"Role": {"PermissionsBoundary": {"PermissionsBoundaryArn": actual}}}
            with self.subTest(actual=actual), self.assertRaises(ValueError):
                deploy.verify_boundary(iam, "111111111111", boundary)
        with self.assertRaises(ValueError):
            deploy.verify_boundary(Mock(), "222222222222", boundary)

    def test_deploy_accepts_exact_preinstalled_boundary(self):
        boundary = "arn:aws:iam::111111111111:policy/ReviewedCollectorBoundary"
        iam = Mock()
        iam.get_role.return_value = {"Role": {"PermissionsBoundary": {"PermissionsBoundaryArn": boundary}}}
        deploy.verify_boundary(iam, "111111111111", boundary)
        iam.get_role.assert_called_once_with(RoleName=COLLECTOR)


if __name__ == "__main__":
    unittest.main()
