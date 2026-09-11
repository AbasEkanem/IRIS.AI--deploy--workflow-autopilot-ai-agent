"""slack_webook.py — Back-compat shim for the historical typo'd module name.

The real module is now correctly spelled ``slack_webhook.py``. This shim keeps any
lingering ``import slack_webook`` working — including the underscore-prefixed names
(``_process_agent_result``, ``_pending_actions``) that ``from … import *``
deliberately skips. Prefer importing from ``slack_webhook`` directly.
"""
from slack_webhook import *  # noqa: F401,F403 - re-export the public API
from slack_webhook import _process_agent_result, _pending_actions  # noqa: F401 - import * skips underscore names
