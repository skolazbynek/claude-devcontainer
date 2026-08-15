"""Chat bridges: edge adapters that carry the mailbox transport to a chat channel.

See docs/impl-mattermost-bridge-plan.md. A bridge is a host-side process with a
mailbox identity of its own; it holds no docker socket privileges beyond a
read-only container listing and gives no container a new capability.
"""
