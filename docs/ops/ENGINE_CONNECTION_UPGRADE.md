# Engine connection upgrade

Saved headers and the full saved `base_url` path now take effect on requests
through the gateway's lifecycle hop. This is intentional: the hop uses the
connection shown on the engine page. Health checks keep their existing paths
when a connection is saved.

Admin schema 11 removes a numeric `config.timeout` of exactly `600` once from
records routed through the lifecycle hop. The old engine page wrote that value
as a default, but the hop ignored it. Honoring it now would cut long generations
at 600 seconds on installations whose owner never chose that limit. Removing
it restores the hop's 1800 second default.

The migration preserves other values, missing timeouts, and direct routes.
A deliberate 600 second limit cannot be distinguished from the old page default;
set 600 again after upgrading if that limit is wanted. Later restarts preserve
that choice.
