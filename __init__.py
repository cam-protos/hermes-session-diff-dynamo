"""Standalone Hermes plugin entry point."""


def register(ctx):
    from .session_diff.recorder import Recorder

    recorder = Recorder(
        terminal_capture=ctx.get_config("terminal_capture", True),
        retention_days=ctx.get_config("retention_days", 30),
    )
    ctx.register_hook("pre_llm_call", recorder.begin_run)
    ctx.register_middleware("tool_execution", recorder.execute)
