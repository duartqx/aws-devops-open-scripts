#!/usr/bin/env python
from argparse import ArgumentParser, Namespace, RawTextHelpFormatter
from typing import Any, Dict, Iterable, List, Union
import json
import os

try:
    from redis import Redis
    from redis.exceptions import ConnectionError
    import boto3  # type: ignore
except ModuleNotFoundError as e:
    import sys

    print(e, file=sys.stderr)
    sys.exit(1)

from datasets import ENVIRONMENTS


class RedisHandler:
    def __init__(self, db: int, **kwargs: Dict[str, Any]) -> None:
        self._r = Redis(db=db, socket_connect_timeout=1, **kwargs)  # pyright: ignore
        self.__bool = None

    def __bool__(self) -> bool:
        if self.__bool is not None:
            # Keeps boolean value cached
            return self.__bool
        try:
            self.__bool = self._r.ping()
        except ConnectionError:
            self.__bool = False
        return self.__bool

    def getkey(self, key: str) -> Dict[str, Any]:
        value = self._r.get(key)
        return json.loads(value.decode()) if value else {}

    def setkey(self, key: str, value: Any, timeout: Union[int, None] = None) -> None:
        self._r.set(key, json.dumps(value))
        if timeout:
            self._r.expire(key, timeout)


class Handler:
    def __init__(self, invalidate: bool = False, **kwargs: Dict[str, Any]) -> None:
        self.invalidate = invalidate
        self._redis = RedisHandler(db=kwargs.pop("db", 15))  # type: ignore
        self._client = self._get_client(**kwargs)

    def get_environment_variables(self, env_name: str, app_name: str) -> Dict[str, Any]:
        env_vars: Dict[str, Any]

        if self.invalidate or not self._redis:
            # Skips getkey operation if set to not use redis or redis is not
            # available on the machine, or the HOST/PORT is wrong
            env_vars = {}
        else:
            # Else tries to look for env_vars cached on redis
            env_vars = self._redis.getkey(f"{env_name}__{app_name}")

        if not env_vars:
            get_env: Dict[str, Any] = self._client.describe_configuration_settings(
                EnvironmentName=env_name, ApplicationName=app_name
            )

            env_vars = {
                e["OptionName"]: e["Value"]
                for e in (  # type: ignore
                    filter(
                        lambda x: x["Namespace"]  # type: ignore
                        == "aws:elasticbeanstalk:application:environment",
                        get_env["ConfigurationSettings"][0]["OptionSettings"],
                    )
                )
            }

            if self._redis:
                self._redis.setkey(  # type: ignore
                    key=f"{env_name}__{app_name}",
                    value=env_vars,
                    timeout=14400,
                )

        return env_vars

    def _get_client(self, **kwargs):
        if (
            kwargs.get("aws_access_key_id")
            and kwargs.get("aws_secret_access_key")
            and kwargs.get("region_name")
        ):
            return boto3.client(  # type: ignore
                "elasticbeanstalk",
                aws_access_key_id=kwargs.get("aws_access_key_id"),
                aws_secret_access_key=kwargs.get("aws_secret_access_key"),
                region_name=kwargs.get("region_name"),
            )
        return boto3.client("elasticbeanstalk")


def get_args() -> Namespace:
    parser = ArgumentParser(
        prog="describe_variables",
        description=(
            "Get environment variables informations about "
            "one or more ElasticBeanstalk Environment."
        ),
        formatter_class=RawTextHelpFormatter,
    )

    # fmt: off
    options: List[Dict[str, Any]] = [
        {
            "opt": ("envs",),
            "help": (
                f"One or More environments from:"
                f"\n{list(ENVIRONMENTS.keys())}".replace("'", "")
            ),
            "nargs": "*",
            "type": str,
        },
        {
            "opt": ("-V", "--variables",),
            "help": "List of variables you want to check the value",
            "nargs": "*",
            "type": str,
        },
        {
            "opt": ("-I", "--invalidate",),
            "help": "Ignores redis and forces refresh with GET request",
            "action": "store_true",
        },
        {
            "opt": ("-i", "--access-id",),
            "help": (
                "AWS access key id, defaults to AWS_ACCESS_KEY_ID "
                "environment variable"
            ),
            "default": os.environ.get("AWS_ACCESS_KEY_ID"),
        },
        {
            "opt": ("-s", "--secret-key",),
            "help": (
                "AWS secret key, defaults to AWS_SECRET_ACCESS_KEY"
                " environment variable"
            ),
            "default": os.environ.get("AWS_SECRET_ACCESS_KEY"),
        },
        {
            "opt": ("-r", "--region-name",),
            "help": "AWS region name, defaults to us-east-1",
            "default": "us-east-1",
        },
    ]
    # fmt: on

    for option in options:
        parser.add_argument(*option.pop("opt"), **option)

    args = parser.parse_args()

    return args


def main(args: Namespace) -> None:
    h = Handler(
        **{
            "aws_access_key_id": args.access_id,
            "aws_secret_access_key": args.secret_key,
            "invalidate": args.invalidate,
            "region_name": args.region_name,
        }
    )

    loop_envs: Iterable[str] = args.envs if args.envs else ENVIRONMENTS.keys()

    for env in loop_envs:
        e: Union[Dict[str, str], None] = ENVIRONMENTS.get(env)

        if e is None:
            continue

        env_variables: Dict[str, Any] = h.get_environment_variables(
            env_name=e["env_name"], app_name=e["app_name"]
        )

        # prints environment name in green
        print("\033[92m{}:\033[00m".format(env))

        if not args.variables:
            for var, value in env_variables.items():
                print(f"{var}={value}")
        elif args.variables:
            for var in args.variables:
                print(f"{var}={env_variables.get(var)}")


if __name__ == "__main__":
    args: Namespace = get_args()
    # use -h for help information about args

    main(args)
