import logging
import os
import requests

from boto3 import client as Client
from botocore.exceptions import ClientError
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from re import sub
from typing import List



APPLICATION_NAME: str = os.environ.get("APPLICATION_NAME", "")
AWS_ACCESS_KEY_ID: str = os.environ.get("ACCESS_KEY_ID", "")
AWS_REGION: str = os.environ.get("AWS_REGION", "")
AWS_SECRET_ACCESS_KEY: str = os.environ.get("SECRET_ACCESS_KEY", "")
JIRA_API_HOST: str = os.environ.get("JIRA_API_HOST", "")
JIRA_TOKEN: str = os.environ.get("JIRA_TOKEN", "")
JIRA_USERNAME: str = os.environ.get("JIRA_USERNAME", "")
SLACK_WEBHOOK_URL: str = os.environ.get("SLACK_WEBHOOK_URL", "")
TO_FROM: str = os.environ.get("TO_FROM", "")


def unquote_list_of_strings(list_of_strings: List[str]):
    """
    Converts a list of strings to a string and removes quotes
    """
    return sub("\"|'", "", str(list_of_strings))


def send_raw_email(subject: str, message: str) -> None:
    """
    Sends an email using AWS Simple Email Service

    Params:
        subject: str
        message: str
    """

    raw_email = {
        "From": TO_FROM,
        "to": TO_FROM,
    }

    msg = MIMEMultipart('mixed', **raw_email)  # pyright: ignore
    msg["Subject"] = subject
    msg_body = MIMEMultipart("alternative")
    msg_body.attach(MIMEText(message, "plain", "utf-8"))
    msg.attach(msg_body)
    
    ses_client = Client(
        "ses",
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        region_name=AWS_REGION,
    )

    try:
        ses_client.send_raw_email(
            Source=TO_FROM,
            Destinations=[TO_FROM],
            RawMessage={
                "Data": msg.as_string(),
            },
        )
    except ClientError as e:
        logging.error(e.response["Error"]["Message"])

    # Posts message to deploy slack channel via slack app webhook
    requests.post(
        SLACK_WEBHOOK_URL,
        headers={"Content-type": "application/json"},
        json={"text": message}
    )

def terminate_ebs() -> None:
    """
    Terminates ElasticBeanstalk dynamic environments every friday at 5pm.

    The function connects to elasticbeanstalk with boto3.client and grabs the
    list of available environments and terminates all that pass the filter. The
    filter is the environment's name not being on a specific list of
    environments that we do not want to close and whose name does not begin with
    "AJ."
    """

    eb_client = Client(
        "elasticbeanstalk",
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        region_name=AWS_REGION,
    )
    eb_envs = eb_client.describe_environments(ApplicationName=APPLICATION_NAME)

    if not "Environments" in eb_envs:
        return

    terminated_environments: List[str] = []
    for env in filter(
        lambda x: x["EnvironmentName"].startswith("AJ") and x["Status"] == "Ready",
        eb_envs["Environments"],
    ):
        # Filters environments and only terminates the ones not in SKIP, that
        # it's name starts with AJ and it's status is Ready
        eb_client.terminate_environment(
            EnvironmentId=env["EnvironmentId"], EnvironmentName=env["EnvironmentName"]
        )
        terminated_environments.append(env["EnvironmentName"])

    if terminated_environments:
        datetime_now = datetime.now()
        send_raw_email(
            subject=f"Ambientes ElasticBeanstalk fechados {datetime_now}",
            message=(
                f"Lista de Ambientes fechados automaticamente em {datetime_now}:"
                f"\n{unquote_list_of_strings(terminated_environments)}"
            ),
        )


def rebuild_ebs() -> None:
    """
    Rebuilds dynamic environments that where terminated automatically if they
    are still with one of these Jira statuses:
        (
            "Atualizado Após Teste / Correção",
            "Em Teste"
        )

    """

    ebs_client = Client(
        "elasticbeanstalk",
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        region_name=AWS_REGION,
    )
    
    # Queries for all jiras that are in testing but not in correction
    jira_query = {
        "jql": (
            """
                project = AJ AND status in (
                    "Atualizado Após Teste / Correção (com cliente)",
                    "Atualizado Após Teste / Correção (dinâmico)",
                    "Em Teste (com cliente)",
                    "Em Teste (dinâmico)"
                )
                ORDER BY created DESC
            """
        ),
        "maxResults": 20,
    }
    jira_response = requests.get(
        f"{JIRA_API_HOST}/rest/api/2/search/",
        params=jira_query,
        headers={"Accept": "application/json"},
        auth=(JIRA_USERNAME, JIRA_TOKEN),
    )

    if jira_response.status_code != 200:
        logging.warn(f"Jira responded with status code {jira_response.status_code}")
        return
    
    # Uses the response from jira to build the EnvironmentNames list of strings
    # (names of environemnts) to make a GET request to elasticbeanstalk that
    # returns information about environments, even if it's already terminated
    eb_envs = ebs_client.describe_environments(
        EnvironmentNames=[
            jira["key"].replace("-", "") for jira in jira_response.json()["issues"]
        ],
        IncludeDeleted=True,
        IncludedDeletedBackTo=datetime.now() - timedelta(days=4)
    )
    
    rebuilt_environments: List[str] = []
    for env in filter(
        lambda x: x["Status"] == "Terminated",
        sorted(eb_envs["Environments"], key=lambda x: x["DateCreated"], reverse=True),
    ):
        # Rebuilds environments that were found with describe_environments and
        # that are Terminated
        # it uses reversed sorted by DateCreated to make sure we only try to
        # rebuild the newest environment of a jira, because it could possibly
        # return one or more environments with the same name
        if env["EnvironmentName"] not in rebuilt_environments:
            ebs_client.rebuild_environment(
                EnvironmentId=env["EnvironmentId"],
                EnvironmentName=env["EnvironmentName"],
            )
            rebuilt_environments.append(env["EnvironmentName"])

    if rebuilt_environments:
        datetime_now = datetime.now()
        send_raw_email(
            subject=f"Ambientes ElasticBeanstalk reabertos {datetime_now}",
            message=(
                f"Lista de Ambientes reabertos automaticamente em {datetime_now}:"
                f"\n{unquote_list_of_strings(rebuilt_environments)}"
            ),
        )


def lambda_handler(event, context) -> None:
    """
    Terminates or Rebuilds elasticbeanstalk environments based on the event
    
    Event Bodies:
        Rebuild: { "rebuild": "1" }
        Terminate: { "terminate": "1" }
    """
    if event.get("rebuild"):
        return rebuild_ebs()
    elif event.get("terminate"):
        return terminate_ebs()
