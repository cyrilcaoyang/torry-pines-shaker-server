"""Torrey Pines Shaker REST server.

Conforms to the AC Organic Self-Driving Lab status spec v1.1 (see
``ac-organic-lab/docs/STATUS_SPEC.md``). The underlying device driver
is `matterlab_shakers.TorreyPinesShaker` from the Matter Lab GitLab
group.

The driver and FastAPI service are imported on demand so callers that
only want the package metadata don't pull in pyserial or FastAPI::

    from torry_pines_shaker_server.api import create_app
    from torry_pines_shaker_server.service import ShakerService
"""

__version__ = "0.1.0"
