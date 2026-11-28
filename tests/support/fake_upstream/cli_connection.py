"""A non-HTTP connection mechanism for exercising the generic owner lifecycle."""
from gateway.provider_connections import ConnectMechanism, ConnectStart, ConnectionFacts, Credential


class CliConnection(ConnectMechanism):
    def __init__(self):
        self.calls = []

    def validate(self, row, *, source):
        if "oauth" in row or "upstream" in row:
            raise ValueError("CLI fixture needs neither sign-in HTTP configuration nor upstream fields")

    async def start(self, ctx, attempt):
        self.calls.append(("start", attempt.owner_id))
        return ConnectStart({"command": "fake-cli sign-in"}, "cli-state", {"ticket": "opaque-cli-ticket"})

    async def finish(self, ctx, attempt, values):
        self.calls.append(("finish", attempt.owner_id))
        assert attempt.mechanism_state == {"ticket": "opaque-cli-ticket"}
        assert values["state"] == attempt.state
        return ConnectionFacts({}, Credential("runner", "fake-cli-session"))

    async def acquire(self, ctx, connection):
        self.calls.append(("acquire", connection["connected_by"]))
        return Credential("runner", "fake-cli-session")

    def configured(self, ctx, connection):
        return connection["status"] == "connected"

    def status(self, ctx, connection):
        self.calls.append(("status", connection["connected_by"]))
        return {"runner_status": "ready"}

    async def disconnect(self, ctx, connection):
        self.calls.append(("disconnect", connection["connected_by"]))
        return {"signed_out": True}


class CliProtocol:
    def validate(self, row, *, source):
        pass

    async def discover_models(self, kind, credential, client_context):
        assert credential.kind == "runner"
        return {}
