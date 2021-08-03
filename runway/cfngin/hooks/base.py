"""Base class for CFNgin hooks."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Optional, Type, Union, cast

from troposphere import Tags

from ...config.models.cfngin import CfnginStackDefinitionModel
from ...utils import BaseModel
from ..actions import deploy
from ..exceptions import StackFailed
from ..stack import Stack
from ..status import COMPLETE, FAILED, PENDING, SKIPPED, SUBMITTED
from .protocols import CfnginHookProtocol

if TYPE_CHECKING:
    from ..._logging import RunwayLogger
    from ...context import CfnginContext
    from ..blueprints.base import Blueprint
    from ..providers.aws.default import Provider
    from ..status import Status

LOGGER = cast("RunwayLogger", logging.getLogger(__name__))


class HookArgsBaseModel(BaseModel):
    """Base model for hook args."""

    tags: Dict[str, str] = {}


class Hook(CfnginHookProtocol):
    """Base class for hooks.

    Not all hooks need to be classes and not all classes need to be hooks.

    Attributes:
        args: Keyword arguments passed to the hook, loaded into a MutableMap object.
        blueprint: Blueprint generated by the hook if it will be deploying a stack.
        context: Context instance. (passed in by CFNgin)
        provider Provider instance. (passed in by CFNgin)
        stack: Stack object if the hook deploys a stack.
        stack_name: Name of the stack created by the hook if a stack is to be created.

    """

    ARGS_PARSER: Type[HookArgsBaseModel] = HookArgsBaseModel

    args: HookArgsBaseModel
    blueprint: Optional[Blueprint] = None
    context: CfnginContext
    provider: Provider
    stack: Optional[Stack] = None
    stack_name: str = "stack"

    def __init__(  # pylint: disable=super-init-not-called
        self, context: CfnginContext, provider: Provider, **kwargs: Any
    ) -> None:
        """Instantiate class.

        Args:
            context: Context instance. (passed in by CFNgin)
            provider: Provider instance. (passed in by CFNgin)

        """
        kwargs.setdefault("tags", {})

        self.args = self.ARGS_PARSER.parse_obj(kwargs)
        self.args.tags.update(context.tags)
        self.context = context
        self.provider = provider
        self._deploy_action = HookDeployAction(self.context, self.provider)
        self._destroy_action = HookDestroyAction(self.context, self.provider)

    @property
    def tags(self) -> Tags:
        """Return tags that should be applied to any resource being created."""
        return Tags(**dict(self.context.tags, **self.args.tags))

    def generate_stack(self, **kwargs: Any) -> Stack:
        """Create a CFNgin Stack object."""
        definition = CfnginStackDefinitionModel.construct(
            name=self.stack_name, tags=self.args.tags, **kwargs
        )
        stack = Stack(definition, self.context)
        stack._blueprint = self.blueprint  # pylint: disable=protected-access
        return stack

    def get_template_description(self, suffix: Optional[str] = None) -> str:
        """Generate a template description.

        Args:
            suffix: Suffix to append to the end of a CloudFormation template
                description.

        """
        template = "Automatically generated by {}"
        if suffix:
            template += " - {}"
            return template.format(self.__class__.__module__, suffix)
        return template.format(self.__class__.__module__)

    def deploy_stack(self, stack: Optional[Stack] = None, wait: bool = False) -> Status:
        """Deploy a stack.

        Args:
            stack: A stack to act on.
            wait: Wither to wait for the stack to complete before returning.

        Returns:
            Ending status of the stack.

        """
        return self._run_stack_action(
            action=self._deploy_action, stack=stack, wait=wait
        )

    def destroy_stack(
        self, stack: Optional[Stack] = None, wait: bool = False
    ) -> Status:
        """Destroy a stack.

        Args:
            stack: A stack to act on.
            wait: Wither to wait for the stack to complete before returning.

        Returns:
            Ending status of the stack.

        """
        return self._run_stack_action(
            action=self._destroy_action, stack=stack, wait=wait
        )

    def post_deploy(self) -> Any:
        """Run during the **post_deploy** stage."""
        raise NotImplementedError

    def post_destroy(self) -> Any:
        """Run during the **post_destroy** stage."""
        raise NotImplementedError

    def pre_deploy(self) -> Any:
        """Run during the **pre_deploy** stage."""
        raise NotImplementedError

    def pre_destroy(self) -> Any:
        """Run during the **pre_destroy** stage."""
        raise NotImplementedError

    @staticmethod
    def _log_stack(stack: Stack, status: Status) -> None:
        """Log stack status. Mimics normal stack deployment.

        Args:
            stack: The stack being logged.
            status: The status being logged.

        """
        msg = f"{stack.name}:{status.name}"
        if status.reason:
            msg += f" ({status.reason})"
        if status.code == SUBMITTED.code:
            LOGGER.notice(msg)
        elif status.code == COMPLETE.code:
            LOGGER.success(msg)
        elif status.code == FAILED.code:
            LOGGER.error(msg)
        else:
            LOGGER.info(msg)

    def _run_stack_action(
        self,
        action: Union[HookDeployAction, HookDestroyAction],
        stack: Optional[Stack] = None,
        wait: bool = False,
    ) -> Status:
        """Run a CFNgin hook modified for use in hooks.

        Args:
            action: Action to be taken against a stack.
            stack: A stack to act on.
            wait: Wither to wait for the stack to complete before returning.

        Returns:
            Ending status of the stack.

        """
        stack = stack or self.stack
        if not stack:
            raise ValueError("stack required")
        status = action.run(stack=stack, status=PENDING)
        self._log_stack(stack, status)

        if wait and status != SKIPPED:
            status = self._wait_for_stack(
                action=action, stack=stack, last_status=status
            )
        return status

    def _wait_for_stack(
        self,
        action: Union[HookDeployAction, HookDestroyAction],
        last_status: Optional[Status] = None,
        stack: Optional[Stack] = None,
        till_reason: Optional[str] = None,
    ):
        """Wait for a CloudFormation stack to complete.

        Args:
            action: Action to be taken against a stack.
            last_status: The last status of the stack.
            stack: A stack that has been acted upon.
            till_reason: Status string to wait for before returning.
                ``COMPLETE`` or ``FAILED`` status will return before this
                condition if found.

        Returns:
            Ending status of the stack.

        Raises:
            StackFailed: Stack is in a failed state.

        """
        status = last_status or SUBMITTED
        stack = stack or self.stack
        if not stack:
            raise ValueError("stack required")

        while True:
            if status in (COMPLETE, FAILED):
                break
            if (till_reason and status.reason) and status.reason == till_reason:
                break
            if last_status and last_status.reason != status.reason:
                # log status changes like rollback
                self._log_stack(stack, status)
                last_status = status
            LOGGER.debug("waiting for stack to complete...")
            status = action.run(stack=stack, status=status)

        self._log_stack(stack, status)
        if status == FAILED:
            raise StackFailed(stack_name=stack.fqn, status_reason=status.reason)
        return status


class HookDeployAction(deploy.Action):
    """Deploy action that can be used from hooks."""

    def __init__(self, context: CfnginContext, provider: Provider):
        """Instantiate class.

        Args:
            context: The context for the current run.
            provider: The provider instance.

        """
        super().__init__(context)
        self._provider = provider

    @property
    def provider(self) -> Provider:
        """Override the inherited property to return the local provider."""
        return self._provider

    def build_provider(self) -> Provider:
        """Override the inherited method to always return local provider."""
        return self._provider

    def run(self, **kwargs: Any) -> Status:  # type: ignore
        """Run the action for one stack."""
        return self._launch_stack(**kwargs)


# the build action has logic to destroy stacks so we can just extend the
# HookDeployAction and change `run` in use the `_destroy_stack` method instead
class HookDestroyAction(HookDeployAction):
    """Destroy action that can be used from hooks."""

    def run(self, **kwargs: Any) -> Status:
        """Run the action for one stack."""
        return self._destroy_stack(**kwargs)
