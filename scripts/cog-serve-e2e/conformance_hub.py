"""TEST-ONLY launcher: the Hub with publish validation relaxed, for the OCI conformance suite.

The suite pushes fixtures that are not Cogs. This script replaces the
publisher's validation, in this process only, with one that accepts any
single manifest. It is not part of the package and there is no setting that
does this.
"""
import os
from datetime import UTC, datetime

import uvicorn
from collab_hub_api.cogs.catalog import STATUS_INDEXED, CogArtifact
from collab_hub_api.cogs.publishing import CogPublisher
from collab_hub_api.config import Config
from collab_hub_api.core import make_app


async def _accept_anything(self, repository, manifest, tags):
    cog_id = "conformance/" + repository.replace("/", "-")
    return CogArtifact(
        source_id=self._source.id,
        host=self._source.host,
        repository=repository,
        digest=manifest.digest,
        status=STATUS_INDEXED,
        tags=tags,
        pushed_at=datetime.now(UTC),
        manifest_media_type=manifest.media_type,
        card={"id": cog_id, "name": repository},
        cog_id=cog_id,
        name=repository,
    )


CogPublisher._validate = _accept_anything
config = Config.parse()
uvicorn.run(make_app(config), host="0.0.0.0", port=int(os.environ["CONF_PORT"]), log_config=None)
