"""Pure routing: which agent a post is for, and what to reject outright.

No network, no filesystem, no docker -- everything here is a function of the post
and the bridge's own state, so the interesting decisions are unit-testable.
"""

import re
from dataclasses import dataclass

COMMAND = "command"
AGENT = "agent"
ERROR = "error"

_MENTION = re.compile(r"^\s*@([A-Za-z0-9_.-]+)\s*(.*)$", re.DOTALL)


@dataclass(frozen=True)
class Route:
    kind: str
    target: str = ""
    text: str = ""
    root_id: str = ""
    error: str = ""


def rejection_reason(
    post: dict, channel_id: str, allowed_user_ids: tuple[str, ...], seen: bool,
    self_user_id: str = "",
) -> str | None:
    """Why this post must be ignored, or None to process it (plan §7).

    Rejections are silent: a refusal post for every join message would be noise,
    and answering a disallowed user tells them the bridge is here.

    *self_user_id* is what stops the bridge answering itself. A bot account's posts
    carry ``from_bot`` and would be caught below anyway, but a token minted on a human
    account posts as that human -- who is necessarily on the allowlist, so every reply
    would come straight back in as a new command.
    """
    if seen:
        return "already processed"
    if self_user_id and post.get("user_id") == self_user_id:
        return "our own post"
    if post.get("channel_id") != channel_id:
        return "other channel"
    if post.get("type"):
        return "system message"
    props = post.get("props") or {}
    if props.get("from_bot") or props.get("from_webhook"):
        return "bot or webhook"
    if post.get("user_id") not in allowed_user_ids:
        return "sender not allowed"
    if post.get("update_at", 0) > post.get("create_at", 0):
        return "edited post"
    if not (post.get("message") or "").strip():
        return "empty message"
    return None


def match_names(token: str, known: list[str]) -> list[str]:
    """Container names matching *token*: exact, then bare slug, then prefix."""
    if token in known:
        return [token]
    suffix = [n for n in known if n.rsplit("_", 1)[-1] == token]
    if suffix:
        return suffix
    return [n for n in known if n.startswith(token)]


def route_post(post: dict, thread_agent: str | None, known: list[str]) -> Route:
    """Bang-command, thread reply, ``@name``, or an error telling the user how to address (plan §6)."""
    text = (post.get("message") or "").strip()
    root_id = post.get("root_id") or post.get("id", "")

    if text.startswith("!"):
        word, _, rest = text[1:].partition(" ")
        return Route(COMMAND, target=word.lower(), text=rest.strip(), root_id=root_id)

    if post.get("root_id") and thread_agent:
        return Route(AGENT, target=thread_agent, text=text, root_id=root_id)

    if (m := _MENTION.match(text)):
        token, body = m.group(1), m.group(2).strip()
        matches = match_names(token, known)
        if not matches:
            return Route(ERROR, root_id=root_id, error=f"No agent matches `{token}`.")
        if len(matches) > 1:
            listed = ", ".join(f"`{n}`" for n in sorted(matches))
            return Route(ERROR, root_id=root_id, error=f"`{token}` is ambiguous: {listed}")
        if not body:
            return Route(ERROR, root_id=root_id, error=f"Nothing to send to `{matches[0]}`.")
        return Route(AGENT, target=matches[0], text=body, root_id=root_id)

    if post.get("root_id"):
        return Route(ERROR, root_id=root_id, error="This thread is not bound to an agent. Start a new one with `@<agent> …`.")

    return Route(ERROR, root_id=root_id, error="Address an agent: `@<agent> your message`. `!fleet` lists them.")


def subject_of(text: str, width: int = 72) -> str:
    """First line, trimmed -- the mailbox wants a subject and chat has no field for one."""
    first = text.strip().splitlines()[0] if text.strip() else "(no subject)"
    return first if len(first) <= width else first[: width - 1] + "…"


def split_output(text: str, limit: int) -> list[str]:
    """Chunk on line boundaries, reopening a code fence that a split would leave open."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    fenced = False

    def flush() -> None:
        nonlocal current, size
        if not current:
            return
        body = "\n".join(current)
        chunks.append(body + "\n```" if fenced else body)
        current = ["```"] if fenced else []
        size = 4 if fenced else 0

    for line in text.split("\n"):
        # A very long single line still has to land somewhere; oversized is better
        # than dropped, and Mattermost will reject it visibly rather than silently.
        if size + len(line) + 1 > limit:
            flush()
        current.append(line)
        size += len(line) + 1
        if line.startswith("```"):
            fenced = not fenced
    flush()
    return [c for c in chunks if c.strip()]
