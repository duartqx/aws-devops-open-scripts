#!/usr/bin/env python
from argparse import ArgumentParser, Namespace
from typing import Any, Dict, List, Pattern, Union

import asyncio
import base64
import httpx
import json
import os
import re

BITBUCKET_API_BASE_URL = os.environ["BITBUCKET_API_BASE_URL"]
BITBUCKET_BASE_URL = os.environ["BITBUCKET_BASE_URL"]
BITBUCKET_TOKEN = os.environ["BITBUCKET_TOKEN"]
JIRA_API_HOST = os.environ["JIRA_API_HOST"]
JIRA_TOKEN = os.environ["JIRA_TOKEN"]
JIRA_USERNAME = os.environ["JIRA_USERNAME"]
JIRA_AUTH: str = base64.b64encode(f"{JIRA_USERNAME}:{JIRA_TOKEN}".encode()).decode()


async def get_bitbucket_response(endpoint: str) -> List[Any]:
    if endpoint.endswith("/"):
        endpoint = endpoint[:-1]

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{BITBUCKET_API_BASE_URL}/{endpoint}/",
            headers={
                "Authorization": f"Bearer {BITBUCKET_TOKEN}",
                "Accept": "application/json",
            },
            params={"sort": "-created_on"},
        )
        if response.status_code != 200:
            return []
        return response.json()["values"]


async def get_jiras_response(
    jiras: Union[List[str], None] = None, only_done: bool = False
) -> List[Dict[str, Any]]:
    if jiras is None:
        jiras = []

    if only_done:
        q = "status IN ('Pronto para produção','Última Revisão de Código')"
    else:
        q = " OR ".join(f"(text ~ {jira} OR issuekey = {jira})" for jira in jiras)

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{JIRA_API_HOST}/rest/api/2/search/",
            json={
                "jql": f"""
                    project = "AJ" AND (
                        {q}
                    ) ORDER BY created DESC
                """,
                "maxResults": 20,
                "fields": [
                    "id",
                    "key",
                    "summary",
                    "status",
                    "issuetype",
                    "reporter",
                    "assignee",
                ],
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Basic {JIRA_AUTH}",
            },
        )
        if response.status_code != 200:
            return []
        return response.json()["issues"]


async def parse_pipelines(
    raw_pipelines: List[Any],
    compiled_regex: Pattern[str],
) -> Dict[str, List[Dict[str, Any]]]:
    pipeline_base_url = f"{BITBUCKET_BASE_URL}/pipelines/results"

    pipelines: Dict[str, List[Dict[str, Any]]] = {}

    for pipeline in raw_pipelines:
        branch: str = pipeline["target"]["ref_name"]
        build_number: int = pipeline["build_number"]
        if compiled_regex.search(branch):
            key: str = branch.split("/")[-1]
            if not pipelines.get(key):
                pipelines[key] = []
            pipelines[key].append(
                {
                    "url": f"{pipeline_base_url}/{build_number}",
                    "branch": branch,
                    "build_number": build_number,
                }
            )
    return pipelines


async def parse_pullrequests(
    raw_pullrequests: List[Any],
    compiled_regex: Pattern[str],
) -> Dict[str, List[Dict[str, str]]]:
    pullrequests: Dict[str, List[Dict[str, str]]] = {}

    for pr in raw_pullrequests:
        branch: str = pr["source"]["branch"]["name"]
        if compiled_regex.search(branch):
            key: str = branch.split("/")[-1]
            if not pullrequests.get(key):
                pullrequests[key] = []
            pullrequests[key].append(
                {"url": pr["links"]["html"]["href"], "branch": branch}
            )
    return pullrequests


async def parse_result(
    jira_response: List[Dict[str, Any]],
    pullrequests_response: List[Dict[str, Any]],
    pipelines_response: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    results: Dict[str, Any] = {}

    for jira in jira_response:
        jira_id = jira["key"]

        results[jira_id] = {
            "id": jira_id,
            "status": jira["fields"]["status"]["name"],
            "type": jira["fields"]["issuetype"]["name"],
            "title": jira["fields"]["summary"],
            "reporter": jira["fields"]["reporter"]["displayName"],
            "assignee": jira["fields"]["assignee"]["displayName"],
            "jira_url": f"{JIRA_API_HOST}/browse/{jira_id}",
            "pullrequests": [],
            "pipelines": [],
        }

    compiled_regex: Pattern[str] = re.compile("|".join(results.keys()))

    pullrequests, pipelines = await asyncio.gather(
        *[
            parse_pullrequests(pullrequests_response, compiled_regex),
            parse_pipelines(pipelines_response, compiled_regex),
        ]
    )

    for key, pipes in pipelines.items():
        if results.get(key):
            filtered_pipelines: Dict[str, Any] = {
                "migrations": [],
                "regular_pr": [],
            }

            last_build_number = 0

            for p in pipes:
                if "migra" in p["branch"]:
                    filtered_pipelines["migrations"].append(p)
                elif last_build_number < p["build_number"]:
                    # Keeps only the pipeline with the biggest build_number if
                    # it's not a migration pipeline
                    filtered_pipelines["regular_pr"] = [p]
                    last_build_number = p["build_number"]

            for p in filtered_pipelines.values():
                results[key]["pipelines"] += p

    for key, prs in pullrequests.items():
        if results.get(key):
            results[key]["pullrequests"] = prs

    return list(results.values())


def format_result_output(result: List[Dict[str, Any]]) -> str:
    output: str = ""
    for issue in result:
        s: str = (
            "\n"
            f"\033[92m{issue['id']}\033[00m: {issue['status']} - {issue['type']}\n"
            f"Title: {issue['title'][:85]}\n"
            f"Reporter: {issue['reporter']}\n"
            f"Assignee: {issue['assignee']}\n"
            f"Jira: {issue['jira_url']}\n"
        )

        for key, title in (
            ("pullrequests", "Pull Requests:\n"),
            ("pipelines", "Pipelines:\n"),
        ):
            if issue.get(key):
                s += title + "".join(
                    f"- {p['url']} ({p['branch']})\n" for p in issue.get(key, [])
                )

        output += s

    return output


def get_args() -> Namespace:
    parser = ArgumentParser(prog="Jira Issues")

    # fmt: off
    options: List[Dict[str, Any]] = [
        {
            "opt": ("jiras",),
            "nargs": "*",
            "type": str,
        },
        {
            "opt": ("-P", "--pronto",),
            "action": "store_true"
        },
        {
            "opt": ("-J", "--json",),
            "action": "store_true"
        },
    ]
    # fmt: on

    for option in options:
        parser.add_argument(*option.pop("opt"), **option)

    args = parser.parse_args()

    return args


async def main() -> None:
    args: Namespace = get_args()

    (
        jira_response,
        pullrequests_response,
        pipelines_resonse,
    ) = await asyncio.gather(
        *[
            get_jiras_response(jiras=args.jiras, only_done=args.pronto),
            get_bitbucket_response("pullrequests"),
            get_bitbucket_response("pipelines"),
        ],
        return_exceptions=True,
    )

    result: List[Dict[str, Any]] = await parse_result(
        jira_response, pullrequests_response, pipelines_resonse
    )

    if args.json:
        print(json.dumps(result))
    else:
        print(format_result_output(result))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
