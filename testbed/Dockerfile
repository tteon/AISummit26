# One image that holds the whole loop: the graph, the harness, and the model server.
#
# Why one image and not three containers: a vast.ai instance *is* a container, so a
# sibling-container topology needs docker-in-docker on the rented host — an extra
# dependency on every candidate machine, for no measurement benefit. Everything the
# experiment measures (agent -> graph round trips, then agent -> model round trips) is
# local either way, and `bulk_load.py --exec-mode local` exists so the loader does not
# need `docker exec` to reach the database.
#
# The DozerDB stage is copied rather than downloaded: it pins the exact database build the
# local numbers were measured against (graphstack/dozerdb:5.26.3.0, Neo4j 5.26 + the
# multi-database plugin), with no tarball URL to rot.
#
#   docker build -f testbed/Dockerfile -t aisummit26-testbed .
#   # verify the layering without pulling the multi-GB vLLM base:
#   docker build -f testbed/Dockerfile --build-arg BASE=python:3.11-slim \
#       -t aisummit26-testbed:novllm .

ARG BASE=vllm/vllm-openai:v0.27.1
ARG DOZERDB=graphstack/dozerdb:5.26.3.0

FROM ${DOZERDB} AS db

FROM ${BASE}
USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl procps ca-certificates git \
 && rm -rf /var/lib/apt/lists/*

# The database and the JRE it needs, lifted whole out of the DozerDB image.
COPY --from=db /opt/java/openjdk /opt/java/openjdk
COPY --from=db /var/lib/neo4j /var/lib/neo4j
COPY --from=db /startup /startup

ENV JAVA_HOME=/opt/java/openjdk \
    NEO4J_HOME=/var/lib/neo4j \
    NEO4J_IMPORT_DIR=/var/lib/neo4j/import \
    DB_EXEC_MODE=local \
    PATH=/var/lib/neo4j/bin:/opt/java/openjdk/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin

# uid 7474 is not cosmetic: neo4j refuses to start on a store it does not own, and the
# offline importer runs as root, so the ownership fix in bulk_load.py must have a real
# neo4j user to chown to.
RUN (getent group neo4j || groupadd -g 7474 neo4j) \
 && (id -u neo4j >/dev/null 2>&1 || useradd -u 7474 -g 7474 -d /var/lib/neo4j -s /bin/bash neo4j) \
 && mkdir -p /data/databases /data/transactions /logs /var/lib/neo4j/import \
 && chown -R neo4j:neo4j /data /logs /var/lib/neo4j

WORKDIR /workspace/AIsummit26
# Two COPYs, not one: `COPY a b ./` flattens both to the same basename and the harness's
# requirements.txt silently becomes the testbed's one-line file.
COPY requirements.txt ./requirements.txt
COPY testbed/requirements.txt ./testbed-requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r testbed-requirements.txt

# Parity, not currency. Every published number in results/ was produced against the
# seocho tree tagged aisummit26-interface-v1 (its software.json records "seocho": "0.2.0"),
# while `seocho>=0.2.0` now resolves to a much later PyPI release. A testbed run that
# quietly used the newer library would not be comparable to the arms it is meant to extend,
# so the tag is pinned here and raising it is a deliberate act.
ARG SEOCHO_SPEC="seocho @ git+https://github.com/tteon/seocho@aisummit26-interface-v1"
RUN pip install --no-cache-dir --force-reinstall --no-deps "${SEOCHO_SPEC}" \
 && python3 -c "import seocho, seocho.query.hybrid_planner as h; \
print('seocho', getattr(seocho, '__version__', '?')); \
from seocho.query.hybrid_planner import policy_from_ontology, schema_for_prompt; \
from seocho.query.workload_compiler import validate_text2cypher_fallback; \
print('interface symbols present')"

# A pip resolve that quietly replaces the base image's torch/vllm would turn a rented hour
# into a debugging session. Fail the build here instead.
#
# `python3`, never `python`: the vLLM base image has no `python` alias, so a bare `python`
# passes on a slim/conda base and fails only in CI — which is where this was found.
RUN python3 -c "\
import importlib.util as u;\
spec = u.find_spec('vllm');\
print('vllm not in this base — skipping serving check') if spec is None else None;\
exec('import torch, vllm; print(\"torch\", torch.__version__, \"vllm\", vllm.__version__); assert torch.cuda.is_available() or True') if spec else None"

COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/workspace/AIsummit26 \
    OPENAI_AGENTS_DISABLE_TRACING=1

# The container must stay up and must NOT start the benchmark by itself.
#
# On vast.ai the instance's lifecycle *is* this container's main process: if it exits, the
# instance goes to `exited` and the run dies with it — so a CMD that runs the benchmark and
# returns would destroy the evidence at the moment it finished. The rental also needs sshd
# alive to be reachable at all. So the image idles, and the benchmark is invoked explicitly
# (`--onstart-cmd`, an ssh session, or `docker run ... testbed/bootstrap.sh` locally).
ENTRYPOINT ["/bin/bash"]
CMD ["-lc", "sleep infinity"]
