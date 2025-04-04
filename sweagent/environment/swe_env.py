from __future__ import annotations

import datetime
import hashlib
import json
import os
import random
import re
import subprocess
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import gymnasium as gym
import yaml
from ghapi.all import GhApi
from git import Repo
from simple_parsing.helpers.serialization.serializable import FrozenSerializable
from simple_parsing.helpers.fields import field
from multi_swe_bench.harness.instance import Instance, Record, Image
from multi_swe_bench.harness.build_dataset import build_image, CliArgs
import docker
import docker.errors
import docker.models.containers
from sweagent import REPO_ROOT
from sweagent.environment.utils import (
    PROCESS_DONE_MARKER_END,
    PROCESS_DONE_MARKER_START,
    InvalidGithubURL,
    copy_anything_to_container,
    copy_file_to_container,
    format_trajectory_markdown,
    get_container,
    get_gh_issue_data,
    get_instances,
    image_exists,
    remove_image,
    parse_gh_issue_url,
    read_with_timeout,
    read_with_timeout_experimental,
    action_hacking
)
from sweagent.utils.config import keys_config
from sweagent.utils.log import default_logger, get_logger

LONG_TIMEOUT = float(keys_config.get("SWE_AGENT_ENV_LONG_TIMEOUT", 500))
AGENT_ACTION_TIMEOUT = float(keys_config.get("SWE_AGENT_ACTION_TIMEOUT", 50))
AGENT_ACTION_NO_OUTPUT_TIMEOUT = float(keys_config.get("SWE_AGENT_ACTION_NO_OUTPUT_TIMEOUT", AGENT_ACTION_TIMEOUT))
PATH_TO_REQS = "/root/requirements.txt"
PATH_TO_ENV_YML = "/root/environment.yml"


@dataclass(frozen=True)
class EnvironmentArguments(FrozenSerializable):
    """Configure data sources and setup instructions for the environment in which we solve the tasks."""
    # Specify the data meta info
    cli_args: CliArgs
    # Specify a branch name or a commit hash to checkout before running the task.
    # Only used when running over a single problem statement/issue.
    base_commit: str | None = None
    # Use a persistent container with this name. After every task, the container will be paused, but not removed.
    # This is useful for speedup when running multiple tasks from the same repositories in a row, as the repositories
    # will have already been cloned and the conda environments will have been installed.
    container_name: str | None = None
    # Try to install the environment before running the task.
    install_environment: bool = True
    # No effect, kept for backwards compatibility.
    timeout: int | None = None
    # Enable environment logger.
    verbose: bool = False
    # Do not use attempt to use a repository mirror from https://github.com/swe-bench.
    no_mirror: bool = True
    # Cache task images to speed up task initialization. This means that the environment will be saved as a
    # docker image for every repository, base commit, and setup combination. This uses quite a bit of disk space
    # but speeds up task initialization significantly when running over multiple issues from the same repository
    # (or using different models for the same issues).
    cache_task_images: bool = False
    # Custom environment setup. Currently only used when data_path points to a single issue.
    # This needs to be either a string pointing to a yaml file (with yaml, yml file extension)
    # or a shell script (with sh extension).
    # See https://princeton-nlp.github.io/SWE-agent/usage/cl_tutorial#environment-setup
    environment_setup: str | None = None
    # Only used when running on single issue. Path to local repository or github repository.
    repo_path: str = ""
     # whether to pre-build all images before running instances or build on the fly
    pre_build_all_images: bool = False
    # remove image after a instance is done
    remove_image: bool = False



    def __post_init__(self):
        if self.timeout is not None:
            default_logger.warning("The 'timeout' argument is deprecated and has no effect.")
        if self.cache_task_images and self.container_name:
            msg = (
                "Not allowed to use persistent container with caching task images "
                "(probably doesn't make sense and takes excessive space)."
            )
            raise ValueError(msg)
        if self.container_name is not None and self.container_name.strip() == "":
            msg = "Set container_name to None if you don't want to use a persistent container."
            raise ValueError(msg)


class EnvHook:
    """Hook to be used in `SWEEnv`.

    Subclass this class, add functionality and add it with `SWEEEnv.add_hook(hook)`.
    This allows to inject custom functionality at different stages of the environment
    lifecycle, in particular to connect SWE-agent to a new interface (like a GUI).
    """

    def on_init(self) -> None:
        """Gets called when the hook is added"""

    def on_copy_repo_started(self, *, repo_type: str, repo_path: str) -> None:
        """Gets called when the repository is being cloned to the container

        Args:
            repo_type: Type of repository. Either 'local' or 'github'
            repo_path: Path to the repository
        """

    def on_install_env_started(self) -> None:
        """Called when we start installing the environment"""

    def on_close(self):
        """Called when the environment is closed"""


class SWEEnv(gym.Env):
    """Gym environment for SWE-bench. This class should handle all communication with the docker container."""

    name = "swe_main"
    # This prefix will be prepended to the image name when caching task images
    cached_image_prefix = "swe-agent-task-env-"

    def __init__(self, args: EnvironmentArguments, log_dir: Path = None):
        super().__init__()
        t0 = time.perf_counter()
        self.args = args
        self.prebuild = args.pre_build_all_images
        self.remove_image = args.remove_image
        self.base_commit: str | None = None
        self.communicate_output: str | None = None
        self.container_name: str | None = args.container_name
        self.install_environment = args.install_environment
        self.logger = get_logger("SWEEnv", log_dir)
        self.persistent = args.container_name is not None
        self.returncode: None | int = None
        if not self.args.verbose:
            # fixme: This creates problems if we have multiple instances of this class
            self.logger.disabled = True

        #: The commit hash of the swe-agent repository
        self.commit_sha = None
        try:
            repo = Repo(REPO_ROOT, search_parent_directories=True)
            self.commit_sha = repo.head.object.hexsha
        except KeyboardInterrupt:
            raise
        except Exception as e:
            self.logger.exception("Failed to get commit hash for this repo: %s", str(e))

        self._github_token: str = keys_config.get("GITHUB_TOKEN", "")  # type: ignore

        # Load Task Instances
        self.data_path = self.args.cli_args.pr_file
        self.data = get_instances(
            self.data_path,
            cli_args=self.args.cli_args,
            prebuild=self.prebuild
        )
        #: Instance we're currently processing. Gets set in self.reset.
        self.record: Record | None = None
        self.logger.info(f"💽 Loaded dataset from {self.data_path}")

        # Establish connection with execution container
        self.image_name = None
        self.container_obj: docker.models.containers.Container | None = None
        self.container: subprocess.Popen | None = None
        # self._reset_container()

        self.idx = 0
        self.clean_multi_line_functions = lambda x: x
        self.hooks: list[EnvHook] = []

        self.logger.debug("Environment initialization took %.2f seconds", time.perf_counter() - t0)

    def _get_cached_task_image_name(self) -> str:
        assert self.record is not None
        inputs: list[str] = [
            self.record.instance.pr.repo,
            self.record.instance.pr.base.sha,
            self.args.environment_setup or "no_setup",
        ]
        tag = hashlib.sha256("".join(inputs).encode()).hexdigest()[:50]
        return f"{self.cached_image_prefix}{tag}"

    def add_hook(self, hook: EnvHook):
        """Add `EnvHook` to the environment.

        This allows to inject custom functionality at different stages of the environment
        lifecycle, in particular to connect SWE-agent to a new interface (like a GUI).
        """
        hook.on_init()
        self.hooks.append(hook)

    @property
    def _repo_name(self) -> str:
        """Name of the local copy of the repository"""
        assert self.record is not None
        return self.record.instance.pr.repo

    def _copy_repo(self) -> str:
        """Clone/copy repository/codebase in container

        Returns:
            folder name of clone
        """
        assert self.container_obj is not None
        assert self.record is not None  # mypy
        if self._github_token:
            token_prefix = f"{self._github_token}@"
        # fixme: This if statement is brittle and should probably be replaced with better logic
        self.logger.info("Trying to clone from non-mirror...")
        clone_url = f"https://{token_prefix}github.com/{self.record.instance.pr.repo}.git"
        clone_method = keys_config.get("SWE_AGENT_CLONE_METHOD", default="sparse", choices=["sparse", "full"])
        if len(self.data) > 1 or self.persistent:
            msg = "Falling back to full cloning method due to multiple instances or persistent container"
            self.logger.debug(msg)
        if clone_method == "full":
            self.communicate_with_handling(
                input=f"git clone {clone_url} {self._repo_name}",
                error_msg="Failed to clone repository from conservative method",
                timeout_duration=LONG_TIMEOUT,
            )
        else:
            base_commit = self.record.instance.pr.base.sha
            self.communicate_with_handling(
                input="&&".join(
                    (
                        f"mkdir {self._repo_name}",
                        f"cd {self._repo_name}",
                        "git init",
                        f"git remote add origin {clone_url}",
                        f"git fetch --depth 1 origin {base_commit}",
                        "git checkout FETCH_HEAD",
                        "cd ..",
                    )
                ),
                error_msg="Failed to clone repository with fast method",
                timeout_duration=LONG_TIMEOUT,
            )
        return self._repo_name

    def reset(self, instance_id: str, apply_test_patch: bool = False) -> tuple[str | None, dict]:
        """
        Function to reset container between each task instance.

        * Clones instance's repository
        * Cleans repository of prior modifications
        * Resets environment variables
        * Check out base commit

        Args:
            instance_id

        Returns:
            observation: output from container
            info: additional information (e.g. debugging information)
        """
        info = {}
        info["commit_sha"] = self.commit_sha

        # Get task instance
        self.record = self.data[instance_id]

        # Set query, gold command
        self.base_commit = self.record.instance.pr.base.sha
        self.query = 'TITLE:\n' + self.record.instance.pr.resolved_issues[0].title + '\n CONTENT:\n' + self.record.instance.pr.resolved_issues[0].body
        self.reward = None

        # build images
        if not self.prebuild:
            self._build_image()

        ### Reset Container ###
        self._reset_container(instance_id)

        # Clone repository if not already cloned
        self.communicate(input="cd /home")
        folders = self.communicate(input="ls").split("\n")
        assert self._repo_name in folders

        # Clean repository of any modifications + Checkout base commit
        for cmd in [
            "echo -n > /root/files_to_edit.txt",
            f"cd {self._repo_name}",
            "export ROOT=$(pwd -P)",
            "git status",
            "git restore .",
            f"git reset --hard {self.base_commit}",
            "git clean -fdxq",
        ]:
            log = self.communicate_with_handling(
                input=cmd,
                error_msg="Failed to clean repository",
                except_error_msgs=['fatal', 'not a git command'],
                timeout_duration=LONG_TIMEOUT,
            )

        # pre-install dependencies for swe-agent ACI tools
        for cmd in [
            "apt-get update",
            'apt-get install -y jq'
        ]:
            self.communicate_with_handling(
                input=cmd,
                error_msg="Failed to install",
                timeout_duration=LONG_TIMEOUT,
                no_output_timeout_duration=LONG_TIMEOUT
            )

        # update .gitignore files
        source_file = Path(REPO_ROOT) / "multi_swe_bench" / "utils" / "gitignores"/ f"{self.record.language}.sh"
        copy_anything_to_container(self.container_obj, str(source_file), "/home/ignore.sh")
        self.communicate('chmod +x /home/ignore.sh')
        self.communicate_with_handling(
            input="bash ../ignore.sh",
            error_msg="Failed to source ignore files"
        )

        # Reset environment variables
        for cmd in [
            'export CURRENT_FILE=""',
            "export CURRENT_LINE=0",
            "export SEARCH_RESULTS=()",
            "export SEARCH_FILES=()",
            "export SEARCH_INDEX=0",
        ]:
            self.communicate_with_handling(
                input=cmd,
                error_msg="Failed to reset environment variables",
            )

        system = self.communicate("uname -s").strip().lower()
        arch = self.communicate("uname -m").strip().lower()
        if system == "linux" and arch == "x86_64":
            self.communicate_with_handling(
                "apt update --allow-insecure-repositories ; apt install build-essential -y --allow-unauthenticated",
                error_msg="Failed to install build-essential",
                timeout_duration=LONG_TIMEOUT,
            )

        # remove the fix.patch if it exists
        self.communicate('rm /home/fix.patch')
        # Write any metadata to info if necessary
        return None, info

    def _apply_test_patch(self):
        """
        Apply test patch for oracle setting
        """
        assert self.record is not None
        path_to_patch = "test.patch"
        with open(path_to_patch, "w") as f:
            f.write(self.record.instance.pr.test_patch)
        subprocess.run(
            f"docker cp {path_to_patch} {self.container_name}:/root/test.patch",
            shell=True,
            check=False,
        )
        self.communicate_with_handling(
            input="git apply /root/test.patch",
            error_msg="Failed to apply test patch correctly",
        )
        os.remove(path_to_patch)

    def step(self, action: str) -> tuple[str | None, int, bool, dict]:
        """
        Runs given action in environment and returns corresponding output

        Args:
            action: command to run in bash shell

        Returns:
            observation:  output from container
            reward: value between 0 and 1 quantifying correctness of output + environment state
            done: whether task is over
            info: additional information (e.g. debugging information)
        """
        info = {}

        observation = ""
        # Handle special actions
        if action.strip() == "skip":
            observation = "Skipped"
            info["exit_status"] = "skipped"
            return observation, 0, True, info
        if action in {"exit_context", "exit_cost", "exit_error", "exit_format", "exit_api"}:
            try:
                observation = self.communicate(input="submit", timeout_duration=AGENT_ACTION_TIMEOUT)
                submission = self.get_submission(observation)
                assert submission is not None and submission.strip() != "", AssertionError("No submission found.")
                self.logger.info(f"Found submission: {submission}")
                info["exit_status"] = f"submitted ({action})"
                info["submission"] = submission
                observation = "Exited (autosubmitted)"
                self.logger.info("Exiting with autosubmission")
                return observation, 0, True, info
            except KeyboardInterrupt:
                raise
            except:
                observation = "Exited"
                info["exit_status"] = action
                return observation, 0, True, info
            
        # do action hacking
        action = action_hacking(action)

        # Attempt to run action in container
        observation = ""
        try:
            observation = self.communicate(
                input=action, 
                timeout_duration=LONG_TIMEOUT, 
                no_output_timeout_duration=AGENT_ACTION_NO_OUTPUT_TIMEOUT
            )
        except TimeoutError as e:
            try:
                observation += e.args[1] if len(e.args) > 1 else ""
                observation += self.interrupt()
                observation += "\nEXECUTION TIMED OUT"
                observation += (
                    f" BECAUSE NO OUTPUT WAS PRODUCED FOR MORE THAN 500 SECONDS.\nPLEASE REFINE YOUR RUNNING COMMAND SO IT WILL PRODUCE OUTPUT IN THE SPECIFIED TIME FRAME."
                )
            except RuntimeError as e:
                observation += e.args[1] if len(e.args) > 1 else ""
                observation += "\nEXECUTION TIMED OUT AND INTERRUPT FAILED."
                info["exit_status"] = "early_exit"
                self.logger.warning(f"Failed to interrupt container: {e}\n")
                self.close()
                return observation, 0, True, info
        except RuntimeError as e:
            observation += "\nCOMMAND FAILED TO EXECUTE."
            info["exit_status"] = "early_exit"
            self.logger.warning(f"Failed to execute command: {e}\n.")
            self.close()
            return observation, 0, True, info
        except BrokenPipeError as e:
            observation += "\nBROKEN PIPE ERROR."
            info["exit_status"] = "early_exit"
            self.logger.error(f"Broken pipe error: {e}\n")
            self.close()
            return observation, 0, True, info
        except Exception:
            observation += "\nEXECUTION FAILED OR COMMAND MALFORMED"
            self.logger.exception("Unknown exception")

        # truncate observation, in case some test information is too large
        if len(observation) > 40000:
            observation = observation[:20000] + "..." + observation[-20000:]


        # Record submission and end episode if `submit` keyword found
        submission = self.get_submission(observation)
        if submission is not None:
            self.logger.info(f"Found submission: {submission}")
            info["exit_status"] = "submitted"
            info["submission"] = submission if submission.strip() != "" else None
            observation = submission if submission.strip() != "" else None
            return observation, 0, True, info
        return observation, 0, False, info

    def close(self) -> None:
        """
        Handle environment shutdown
        """
        self.logger.info("Beginning environment shutdown...")
        try:
            self.communicate(input="exit")
        except KeyboardInterrupt:
            raise
        except:
            self.logger.warning("Errors when exiting container", exc_info=True)
        assert self.container is not None  # mypy
        self.container.terminate()
        if self.container_obj is None:
            pass
        elif self.persistent:
            # stopping is Podman specific, but doesn't hurt to include
            # https://stackoverflow.com/a/32428199/
            # Sleeping to avoid https://github.com/princeton-nlp/SWE-agent/issues/496 ??
            time.sleep(0.1)
            if self.container_obj.status not in {"paused", "exited", "dead", "stopping"}:
                try:
                    self.container_obj.pause()
                except Exception:
                    self.logger.warning("Failed to pause container.", exc_info=True)
                except KeyboardInterrupt:
                    raise
                else:
                    self.logger.info("Agent container paused")
            else:
                self.logger.info(f"Agent container status: {self.container_obj.status}")
        else:
            try:
                self.container_obj.remove(force=True)
            except KeyboardInterrupt:
                raise
            except Exception:
                self.logger.warning("Failed to remove container", exc_info=True)
            else:
                self.logger.info("Agent container stopped")
        for hook in self.hooks:
            hook.on_close()

    # MARK: Helper functions #

    def _build_image(self) -> None:
        instance = self.record.instance
        self.logger.info(f"Building image for {instance.name()}")
        build_image(
            instance.dependency(),
            cli=self.args.cli_args,
            logger=self.logger
        )
        


    def _reset_container(self, instance_id) -> None:
        if self.container is not None:
            try:
                self.container.terminate()
            except KeyboardInterrupt:
                raise
            except:
                self.logger.warning("Failed to terminate container", exc_info=True)
            else:
                self.logger.debug("Terminated container")
        image_full_name = self.record.instance.name()
        self._init_container(image_full_name)
        self._init_scripts()

    def reset_container(self, instance_id) -> None:
        self.close()
        self.container = None
        self.container_obj = None
        self._reset_container(instance_id)

    @staticmethod
    def _get_container_name(image_name: str) -> str:
        """Return name of container"""
        process_id = str(os.getpid())
        current_time = str(datetime.datetime.now())
        unique_string = current_time + process_id
        hash_object = hashlib.sha256(unique_string.encode())
        image_name_sanitized = image_name.replace("/", "-")
        image_name_sanitized = image_name_sanitized.replace(":", "-")
        return f"{image_name_sanitized}-{hash_object.hexdigest()[:10]}"

    def _init_container(self, cached_image: str | None = None) -> None:
        """
        Handles container initialization. Defines container name and creates it.
        If cached_image is provided, it will use that image name instead of the default.
        """
        image_name = self.image_name
        if cached_image is not None:
            image_name = cached_image
            self.logger.info(f"Using cached image: {image_name}")
        if self.persistent:
            assert self.container_name is not None
        else:
            # Make sure that we get a new container name just in case removing didn't work.
            # Might be a fix for https://github.com/princeton-nlp/SWE-agent/issues/451
            self.container_name = self._get_container_name(image_name)
        self.container, self.parent_pids = get_container(self.container_name, image_name, persistent=self.persistent)
        try:
            client = docker.from_env(timeout=600)
        except docker.errors.DockerException as e:
            if "Error while fetching server API version" in str(e):
                msg = "Docker is not running. Please start Docker and try again."
            else:
                msg = "Unknown docker exception occurred. Are you sure docker is running?"
            raise RuntimeError(msg) from e
        t0 = time.time()
        self.container_obj = None
        while time.time() - t0 < 60:
            try:
                self.container_obj = client.containers.get(self.container_name)
            except docker.errors.NotFound:
                self.logger.debug("Couldn't find container. Let's wait and retry.")
                time.sleep(1)
            else:
                break
        else:
            print(f"{self.persistent=}")
            available_containers = client.containers.list(all=True)
            available_containers_info = json.dumps([str(c.attrs) for c in available_containers], indent=2)
            print(available_containers_info)
            msg = "Failed to get container object."
            raise RuntimeError(msg)
        self.logger.info("🌱 Environment Initialized")

    def _init_scripts(self):
        """
        Initialize custom commands within container
        """
        self.communicate_with_handling(
            "mkdir -p /root/commands",
            error_msg="Failed to create commands directory",
        )
        self.communicate_with_handling(
            "touch /root/commands/__init__.py",
            error_msg="Failed to create __init__.py",
        )
        self.communicate_with_handling(
            "export PATH=$PATH:/root/commands",
            error_msg="Failed to add commands directory to PATH",
        )

    def _communicate_experimental(
        self,
        input: str,
        timeout_duration=25,
        no_output_timeout_duration: int | float = 25,
    ) -> str:
        """Experimental version of `_communicate`"""
        assert self.container is not None
        command_suffix = f"echo {PROCESS_DONE_MARKER_START}$?{PROCESS_DONE_MARKER_END}\n"
        try:
            self.returncode = None
            cmd = input if input.endswith("\n") else input + "\n"
            cmd += command_suffix
            os.write(self.container.stdin.fileno(), cmd.encode())
            time.sleep(0.03)
            self.container.stdin.flush()
        except BrokenPipeError:
            traceback.print_exc()
            self.logger.error("Failed to communicate with container. Check docker logs for more information.")
            msg = "Failed to communicate with container"
            raise RuntimeError(msg)

        buffer, exit_code = read_with_timeout_experimental(self.container, timeout_duration, no_output_timeout_duration)
        self.returncode = int(exit_code)
        return buffer

    def _communicate(
        self,
        input: str,
        timeout_duration=25,
        no_output_timeout_duration: int | float = 25,
    ) -> str:
        assert self.container is not None
        communicate_method = keys_config.get(
            "SWE_AGENT_COMMUNICATE_METHOD", default="end-marker", choices=["end-marker", "processes"]
        )
        if communicate_method == "end-marker":
            return self._communicate_experimental(input, timeout_duration, no_output_timeout_duration)
        try:
            self.returncode = None
            cmd = input if input.endswith("\n") else input + "\n"
            os.write(self.container.stdin.fileno(), cmd.encode())
            time.sleep(0.1)
            self.container.stdin.flush()
        except BrokenPipeError:
            traceback.print_exc()
            self.logger.error("Failed to communicate with container. Check docker logs for more information.")
            msg = "Failed to communicate with container"
            raise RuntimeError(msg)
        try:
            buffer = read_with_timeout(self.container, self.get_pids, timeout_duration)
            self.container.stdin.write("echo $?\n")
            time.sleep(0.1)
            self.container.stdin.flush()
            exit_code = read_with_timeout(self.container, self.get_pids, 5).strip()
        except Exception as e:
            self.logger.error(f"Read with timeout failed on input:\n---\n{input}\n---")
            raise e
        if not exit_code.isdigit():
            msg = f"Container crashed. Failed to get exit code. Output:\n---\n{buffer}\n---"
            raise RuntimeError(msg)
        self.returncode = int(exit_code)
        return buffer

    def _check_syntax(self, input: str):
        """
        Saves environment variables to file
        """
        output = self._communicate(f"/bin/bash -n <<'EOF'\n{input}\nEOF\n")
        return output, self.returncode == 0

    def communicate(
        self,
        input: str,
        timeout_duration=25,
        no_output_timeout_duration: int | float | None = 25,
    ) -> str:
        """
        Sends input to container and returns output

        Args:
            input: input to send to container

        Returns:
            output: output from container
        """
        if input.strip() != "exit":
            output, valid = self._check_syntax(input)
            if not valid:
                return output  # shows syntax errors
            output = self._communicate(
                input,
                timeout_duration=timeout_duration,
                no_output_timeout_duration=no_output_timeout_duration
            )
            self.communicate_output = output
            return output
        else:
            self.container.terminate()
            self.returncode = 0
            self.communicate_output = ""
            return ""

    def communicate_with_handling(self, input: str, error_msg: str, timeout_duration=25, no_output_timeout_duration= 25, except_error_msgs = []) -> str:
        """
        Wrapper for communicate function that raises error if return code is non-zero

        Args:
            input: input to send to container
            error_msg: error message to raise if return code is non-zero
            timeout_duration: duration to wait for output

        Returns:
            output: output from container
        """
        logs = self.communicate(input, timeout_duration=timeout_duration, no_output_timeout_duration=no_output_timeout_duration)
        if self.returncode != 0:
            if any( caught_err in logs for caught_err in except_error_msgs):
                self.logger.warning(f'the error message is in exception, some adjustmens will be acted to the commands.')
                return logs
            self.logger.error(f"{error_msg}: {logs}")
            self.close()
            msg = f"{error_msg}: {logs}"
            raise RuntimeError(msg)
        return logs

    def get_available_actions(self) -> list[str]:
        """
        Returns list of available actions in current environment state

        Currently not in use.
        """
        return []

    def get_pids(self, all_pids=False) -> list[str]:
        """
        Gets list of processes running inside docker container

        Args:
            all_pids: whether to return all pids, or whether to exclude the main-process attached pid,
                and parent PIDs

        Returns:
            list of PIDs
        """
        pids = self.container_obj.exec_run("ps -eo pid,comm --no-headers").output.decode().split("\n")
        pids = [x.split() for x in pids if x]
        if not all_pids:
            pids = [x for x in pids if x[1] not in ["ps", 'npm', 'yarn', 'sh'] and x[0] not in self.parent_pids]
        return pids

    def get_submission(self, output: str) -> str:
        """
        Function for extracting diff patch submission at the end of an episode.

        Args:
            output: `submit` observation

        Returns:
            submission: diff patch submission
        """
        pattern = r"\<\<SUBMISSION\|\|(.*)\|\|SUBMISSION\>\>"
        match = re.search(pattern, output, re.DOTALL)
        if match is None:
            return None
        return match.group(1)

    def run_shell_script(self, script_path: Path, *, location: str) -> None:
        """Run custom script supplied by user at `script_path`

        Args:
            script_path: path to script file
            location: location of script file 'host' or 'container'
        """
        if location == "host":
            return self._run_shell_script_host(script_path)
        elif location == "container":
            raise NotImplementedError
        msg = f"Invalid 'location': {location}"
        raise ValueError(msg)

    def _run_shell_script_host(self, script_path: Path) -> None:
        """Run shell script file (located on host) in container"""
        if not script_path.is_file():
            msg = f"Script not found at {script_path}"
            raise FileNotFoundError(msg)
        shell_commands = Path(script_path).read_text().splitlines(keepends=True)
        for i, cmd in enumerate(shell_commands):
            self.communicate_with_handling(
                cmd,
                error_msg=f"Failed to execute line {i}.",
                timeout_duration=LONG_TIMEOUT,
            )

    def _get_install_configs(self) -> dict | None:
        """Return config for environment setup"""
        assert self.record is not None  # mypy
        if (
            self.record["problem_statement_source"] != "swe-bench" or self.record["repo_type"] == "local"
        ) and self.args.environment_setup is None:
            self.logger.warning(
                "install_environment is set to True, but the data path is a GitHub URL "
                "without an environment config file (environment_config key/flag). "
                "Skipping conda environment installation.",
            )
            return None
        if self.args.environment_setup is not None:
            assert isinstance(self.args.environment_setup, (str, os.PathLike))
            if Path(self.args.environment_setup).suffix in [".yml", ".yaml"]:
                try:
                    return yaml.safe_load(Path(self.args.environment_setup).read_text())
                except Exception as e:
                    msg = "Environment config file needs to be a yaml file"
                    raise ValueError(msg) from e
            elif Path(self.args.environment_setup).suffix == ".sh":
                return {
                    "shell_script_path": self.args.environment_setup,
                }
            else:
                msg = "Environment config file needs to be a yaml file or shell script"
                raise ValueError(msg)
        else:
            return None

    def _conda_environment_exists(self, env_name: str) -> bool:
        env_check = self.communicate(f"conda env list | grep {env_name}", timeout_duration=LONG_TIMEOUT)
        return env_check.strip() != ""

    def add_commands(self, commands: list[dict]) -> None:
        """
        Adds custom commands to container
        """
        for command in commands:
            name = command["name"]
            contents = command["contents"]
            copy_file_to_container(self.container_obj, contents, f"/root/commands/{name}")
            if command["type"] == "source_file":
                self.communicate_with_handling(
                    f"source /root/commands/{name}",
                    error_msg=(
                        f"Failed to source {name}. If you meant to make a script,"
                        " start the file with a shebang (e.g. #!/usr/bin/env python)."
                    ),
                )
            elif command["type"] == "script":
                self.communicate_with_handling(
                    f"chmod +x /root/commands/{name}",
                    error_msg=f"Failed to chmod {name}",
                )
            elif command["type"] == "utility":
                # nothing to do for utility scripts
                pass
            else:
                msg = f"Invalid command type: {command['type']}"
                raise ValueError(msg)

    def interrupt(self):
        """
        Send interrupt signal to container and exhaust stdout buffer with a communicate call
        """
        assert self.container is not None
        assert self.container_obj is not None
        pids = self.get_pids()
        for p in reversed(pids):
            # We need to avoid to kill the main process which is in the small pid
            pid = p[0]
            self.container_obj.exec_run(f"kill -9 {pid}")
        observation = ""
        try:
            observation += read_with_timeout(self.container, self.get_pids, 20)
        except TimeoutError:
            pass
        try:
            # This is a workaround because of bash behaviour
            # when sometimes we get the prints of Killed after we press some "Enter" in stdin
            self.communicate(input="echo 'interrupted'", timeout_duration=5)
            output = self.communicate(input="echo 'interrupted'", timeout_duration=5)
            assert output.strip().endswith("interrupted"), "container health check failed"
        except TimeoutError:
            msg = "Failed to interrupt container"
            raise RuntimeError(msg)
        return observation
        
    def on_run_done(self):
        self.close()
        if self.remove_image:
            image_name: str = self.record.instance.dependency().image_full_name()
            self.logger.info(f"Removing image of {image_name}")
            remove_image(image_name)


    def open_pr(self, *, trajectory, _dry_run: bool = False):
        """Create PR to repository

        Args:
            trajectory: Trajectory of actions taken by the agent
            _dry_run: Whether to actually push anything or just simulate it
        """
        self.logger.info("Opening PR")
        # TODO: have better way of handling this
        # Adding random string suffix to avoid name conflicts if we had a previously failed run
        issue_url = self.args.data_path
        try:
            issue = get_gh_issue_data(issue_url, token=self._github_token)
        except InvalidGithubURL as e:
            msg = "Data path must be a github issue URL if --open_pr is set."
            raise ValueError(msg) from e
        branch_name = f"swe-agent-fix-#{issue.number}-" + str(random.random())[2:10]

        self.communicate_with_handling(
            input="rm -f model.patch",
            error_msg="Failed to remove model patch",
            timeout_duration=10,
        )
        self.communicate_with_handling(
            input=f"git checkout -b {branch_name}",
            error_msg="Failed to switch to new branch",
            timeout_duration=10,
        )
        self.communicate_with_handling(
            input="git add .",
            error_msg="Failed to add commits",
            timeout_duration=10,
        )
        dry_run_flag = "--allow-empty" if _dry_run else ""
        self.communicate_with_handling(
            input=f"git commit -m 'Fix: {issue.title}' -m 'Closes #{issue.number}' {dry_run_flag}",
            error_msg="Failed to commit changes",
            timeout_duration=10,
        )

        owner, repo, _ = parse_gh_issue_url(issue_url)
        # If `--repo_path` was specified with a different github URL, then the record will contain
        # the forking user
        assert self.record is not None
        if self.record["repo_type"] != "github":
            # We already validated that `--data_path` is a github issue URL
            # so this is the only case where we can reach here
            msg = "--repo_path must point to a github URL if --open_pr is set"
            raise ValueError(msg)
        forker, _ = self.record["repo"].split("/")
        head = branch_name
        remote = "origin"
        if forker != owner:
            head = f"{forker}:{branch_name}"
            token_prefix = ""
            if self._github_token:
                token_prefix = f"{self._github_token}@"
            fork_url = f"https://{token_prefix}github.com/{forker}/{repo}.git"
            self.logger.debug(f"Using fork: {fork_url}")
            self.communicate_with_handling(
                input=f"git remote add fork {fork_url}",
                error_msg="Failed to create new git remote",
                timeout_duration=10,
            )
            remote = "fork"
        dry_run_prefix = "echo " if _dry_run else ""
        self.communicate_with_handling(
            input=f"{dry_run_prefix} git push {remote} {branch_name}",
            error_msg=(
                "Failed to push branch to remote. Please check your token and permissions. "
                "You might want to push to a fork with the push_gh_repo_url option."
            ),
            timeout_duration=10,
        )
        body = (
            f"This is a PR opened by AI tool [SWE Agent](https://github.com/princeton-nlp/SWE-agent/) "
            f"to close [#{issue.number}]({issue_url}) ({issue.title}).\n\nCloses #{issue.number}."
        )
        body += "\n\n" + format_trajectory_markdown(trajectory)
        api = GhApi(token=self._github_token)
        if not _dry_run:
            pr_info = api.pulls.create(
                owner=owner,
                repo=repo,
                title=f"SWE-agent[bot] PR to fix: {issue.title}",
                head=head,
                base="main",
                body=body,
                draft=True,
            )
            self.logger.info(
                f"🎉 PR created as a draft at {pr_info.html_url}. Please review it carefully, push "
                "any required changes onto the branch and then click "
                "'Ready for Review' to bring it to the attention of the maintainers.",
            )
