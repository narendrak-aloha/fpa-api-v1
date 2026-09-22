"""The seeded identities, by user id.

Every user id is a UUID written as 32 lower-case hex characters, no dashes
(``uuid.uuid4().hex``); migration 020 refuses anything else. A person who
signs up gets a random one. These seven were generated once and are fixed,
because the seed must be repeatable and the dev tokens, the tests and the
recorded workflow histories all have to name the same people on every
fresh stack.

db/seed.yaml carries the same values; tests/test_seed_model.py checks the two agree.
"""

SUPERADMIN = "3d48cdee84ad4c80a270d2eed04ed65e"  # test@superadmin.com
PLANNER = "44cb9d21b3704eb6b9c7c14cfb2d1d80"     # test@planner.com
CONTROLLER = "e2a112d82c994c3ea08f65f1b78b4056"  # test@controller.com
CFO = "fd40d13f7e8f485ea53c6a389a939f99"         # test@cfo.com
ANALYST_PL = "19e78ea2addf472fb1083c080ea9f6a6"  # test@analyst.com, Poland only
SERVICE = "3e91198b720f46579ad74b3aac146ee8"     # the Temporal workflow
AGENT = "1dbad3bde4364851ac7e421ecd5b967a"       # the Agno team

# The ids the codebase used before migration 020, for reading old logs and docs.
LEGACY = {
    "u-admin": SUPERADMIN, "u-planner": PLANNER, "u-controller": CONTROLLER, "u-cfo": CFO,
    "u-analyst-pl": ANALYST_PL, "svc-temporal": SERVICE, "agent-fpa-team": AGENT,
}
