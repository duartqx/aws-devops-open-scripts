#!/usr/bin/env python
from typing import Any, Dict, List
import boto3

# Rule creation command
# aws events put-rule --name "MyEBDeployRule" --event-pattern
# "{\\"source\\":[\\"aws.elasticbeanstalk\\"],\\"detail-type\\":
# [\\"Elastic Beanstalk Environment Update\\"], \
# \\"detail\\":{\\"Status\\":[\\"Ready\\"],\\"ApplicationName\\":\
# [\\"my-application-name\\"],\\"EnvironmentName\\":[\\"my-environment-name\\"]}}"

def lambda_handler(event, context):
    client = boto3.client("ec2")
    env_list: List[str] = event["detail"]["EnvironmentName"]

    desc_instance: Dict[str, Any] = client.describe_instances(
        Filters=[{"Name": "tag:Name", "Values": env_list}]
    )
    instance: Dict[str, Any] = desc_instance["Reservations"][0]["Instances"][0]
    network_interface_id: str = instance["NetworkInterfaces"][0]["NetworkInterfaceId"]

    desc_address: Dict[str, Any] = client.describe_addresses(
        Filters=[{"Name": "tag:Name", "Values": env_list}]
    )
    allocation_id = desc_address["Addresses"][0]["AllocationId"]

    return client.associate_address(
        AllocationId=allocation_id,
        NetworkInterfaceId=network_interface_id,
        AllowReassociation=True,
    )
