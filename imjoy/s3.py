import secrets
import sys
import logging
import string
from imjoy.core import WorkspaceInfo

from imjoy.minio import MinioClient

logging.basicConfig(stream=sys.stdout)
logger = logging.getLogger("s3")
logger.setLevel(logging.INFO)


def generate_password():
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for i in range(20))


class S3Controller:
    def __init__(
        self,
        event_bus,
        core_interface,
        endpoint_url=None,
        access_key_id=None,
        secret_access_key=None,
        default_bucket="imjoy-workspaces",
    ):
        self.endpoint_url = endpoint_url
        self.mc = MinioClient(
            endpoint_url,
            access_key_id,
            secret_access_key,
        )
        self.core_interface = core_interface
        self.default_bucket = default_bucket
        s3 = self.mc.get_resource_sync()
        bucket = s3.Bucket(self.default_bucket)
        if bucket not in s3.buckets.all():
            bucket.create()
            logger.info("Bucket created: %s", self.default_bucket)

        self.mc.admin_user_add("admin", generate_password())
        core_interface.register_interface("get_s3_controller", self.get_s3_controller)
        core_interface.register_interface("getS3Controller", self.get_s3_controller)

        event_bus.on("workspace_registered", self.setup_workspace)
        event_bus.on("workspace_unregistered", self.cleanup_workspace)
        event_bus.on("user_connected", self.setup_user)
        event_bus.on("plugin_registered", self.setup_plugin)

    def setup_user(self, user_info):
        try:
            self.mc.admin_group_info(user_info.id)
        except Exception:
            # Note: we don't store the credentials, it can only be regenerated
            self.mc.admin_user_add(user_info.id, generate_password())

    def setup_plugin(self, plugin):
        self.mc.admin_group_add(plugin.workspace.name, plugin.user_info.id)

    def cleanup_workspace(self, workspace):
        # TODO: if the program shutdown unexcpetedly, we need to clean it up
        self.mc.admin_group_remove(workspace.name)

    def setup_workspace(self, workspace):
        self.mc.admin_group_add(workspace.name, "admin")
        policy_name = "policy-ws-" + workspace.name
        # policy example: https://aws.amazon.com/premiumsupport/knowledge-center/iam-s3-user-specific-folder/
        self.mc.admin_policy_add(
            policy_name,
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "AllowUserToSeeTheBucketInTheConsole",
                        "Action": ["s3:ListAllMyBuckets", "s3:GetBucketLocation"],
                        "Effect": "Allow",
                        "Resource": [f"arn:aws:s3:::{self.default_bucket}"],
                    },
                    {
                        "Sid": "AllowListingOfWorkspaceFolder",
                        "Action": ["s3:ListBucket"],
                        "Effect": "Allow",
                        "Resource": [f"arn:aws:s3:::{self.default_bucket}"],
                        "Condition": {
                            "StringLike": {"s3:prefix": [f"{workspace.name}/*"]}
                        },
                    },
                    {
                        "Sid": "AllowAllS3ActionsInWorkspaceFolder",
                        "Action": ["s3:*"],
                        "Effect": "Allow",
                        "Resource": [
                            f"arn:aws:s3:::{self.default_bucket}/{workspace.name}/*"
                        ],
                    },
                ],
            },
        )

        self.mc.admin_policy_set(policy_name, group=workspace.name)

    def generate_credential(self):
        user_info = self.core_interface.current_user.get()
        workspace = self.core_interface.current_workspace.get()
        password = generate_password()
        self.mc.admin_user_add(user_info.id, password)
        return {
            "endpoint_url": self.endpoint_url,
            "access_key_id": user_info.id,
            "secret_access_key": password,
            "bucket": self.default_bucket,
            "prefix": workspace.name + "/",  # important to have the trailing slash
        }

    def get_s3_controller(self):
        return {"_rintf": True, "generate_credential": self.generate_credential}
