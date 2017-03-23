""" application config
"""
from types import SimpleNamespace

bootstrap = SimpleNamespace(
    # Is bootstrap running
    running=False,

    # Attached output
    output=None
)

maas = SimpleNamespace(
    # Client
    client=None,

    # API key
    api_key=None,

    # API Endpoint
    endpoint=None
)

juju = SimpleNamespace(
    # Client
    client=None,

    # Is authenticated?
    authenticated=False
)

app = SimpleNamespace(
    # Juju bootstrap details
    bootstrap=bootstrap,

    # MAAS client
    maas=maas,

    # Juju Client
    juju=juju,

    # The conjure-up UI framework
    ui=None,

    # Contains metadata and spell name
    config=None,

    # List of multiple bundles, usually from a charmstore search
    bundles=None,

    # Selected bundle from a Variant view
    current_bundle=None,

    # cli opts
    argv=None,

    # Current Juju model being used
    current_model=None,

    # Current Juju controller selected
    current_controller=None,

    # Current Juju cloud selected
    current_cloud=None,

    # Current Juju cloud region
    current_region=None,

    # Session ID for current deployment
    session_id=None,

    # Application logger
    log=None,

    # Charm store metadata API client
    metadata_controller=None,

    # Application environment passed to processing steps
    env=None,

    # Did deployment complete
    complete=False,

    # Run in non interactive mode
    headless=False,

    # Remote endpoint type (An enum, see download.py)
    endpoint_type=None,

    # Flag so we only show the initial cause even if future async
    # events also raise cascading errors
    showing_error=False)
