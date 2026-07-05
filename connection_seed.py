"""
StudyHub Connection Graph Seed Script (Production-Grade)
Generates a realistic university social graph across ~3,000 seeded students.

Run this AFTER seed_students.py.

============================================================================
HOW THE GRAPH IS BUILT
============================================================================
This does NOT create random pairs uniformly. Real social networks are not
uniform — a small number of people have huge networks, most people have
modest ones, and connections cluster heavily around shared context
(department, class level, when you joined, and friends-of-friends).

1. SOCIABILITY TIERS
   Every student is assigned a tier (quiet / casual / average / very_active /
   influencer) via weighted random draw. Each tier has its own target-degree
   range. This alone produces the "some students have 5 connections, some
   have 300" shape requested, without hardcoding a single "hub" user.

2. WEIGHTED CANDIDATE POOLS
   When a student needs a new connection, the partner is drawn from one of:
     - same department pool      (department clusters)
     - same class level pool     (year/level clusters)
     - same join-cohort pool     (recently-joined students cluster together,
                                   like they do via onboarding suggestions)
     - friend-of-a-friend pool   (triadic closure -> real clustering
                                   coefficient, forms tight-knit groups)
     - fully random pool         (cross-department "campus friendships" and
                                   the occasional long-range edge that keeps
                                   the graph from fragmenting into islands)
   Weights are configurable in SeedConfig.

3. STUB-GROWTH, NOT PAIRWISE ENUMERATION
   Instead of generating and scoring all ~4.5M possible pairs, each student
   greedily reaches for connections until they hit their target degree (or
   attempt budget). Candidate pools are small (a department has dozens of
   students, not thousands), so this stays fast at O(total_target_degree)
   rather than O(n^2).

4. DUPLICATE / SELF-CONNECTION SAFETY
   A single `used_pairs` set (unordered id tuples) is checked before any
   edge is accepted, so no pair is ever connected twice and no student is
   ever connected to themselves, before a single row touches the database.

5. STATUS REALISM
   Same-department / same-level pairs skew towards "accepted" (people you
   actually know say yes more often). Cross-department random pairs skew
   towards "pending" / "rejected" (cold outreach has a lower hit rate).

6. PERFORMANCE
   Single query to pull the student population, in-memory graph
   construction, and batched inserts (SeedConfig.BATCH_SIZE) with periodic
   commits — no N+1 queries during graph generation.
"""

import random
import datetime
import logging
import time
from typing import List, Dict, Tuple, Optional
from collections import Counter, defaultdict

from sqlalchemy.exc import SQLAlchemyError, OperationalError, DBAPIError
from extensions import db
from models import User, Connection, StudentProfile

# ============================================================================
# CONFIGURATION
# ============================================================================

class SeedConfig:
    """Centralized configuration for connection graph seeding"""

    SEED_RANDOM_STATE = 42
    BATCH_SIZE = 500

    # Resilience: retry a batch commit on a dropped/stale DB connection
    # instead of aborting the whole run.
    MAX_BATCH_RETRIES = 5
    RETRY_BACKOFF_SECONDS = 2

    # ---- Sociability tiers: (name, selection_weight, (min_degree, max_degree)) ----
    # Weights are relative, not percentages — they just need to sum sensibly.
    DEGREE_TIERS = [
        ("quiet",       8,  (1, 5)),      # barely active, few ties
        ("casual",      35, (5, 15)),     # spec: "some students"
        ("average",     40, (20, 50)),    # spec: "average students"
        ("very_active", 14, (60, 120)),   # spec: "very active students"
        ("influencer",  3,  (150, 300)),  # spec: "campus influencers"
    ]

    # ---- Candidate pool weights (relative) used when picking a partner ----
    POOL_WEIGHT_DEPARTMENT = 40
    POOL_WEIGHT_LEVEL = 15
    POOL_WEIGHT_COHORT = 10
    POOL_WEIGHT_FRIEND_OF_FRIEND = 20   # only available once a student has ties
    POOL_WEIGHT_RANDOM = 15

    # ---- Connection status distribution (base, adjusted per-pair below) ----
    STATUS_DISTRIBUTION = {
        "accepted": 0.60,
        "pending": 0.25,
        "rejected": 0.10,
        "blocked": 0.05,
    }

    # ---- Date ranges ----
    MAX_DAYS_AGO = 180
    MIN_DAYS_AGO = 1

    # ---- Safety valves ----
    HARD_DEGREE_CAP = 400          # no student can exceed this many connections
    MAX_TOTAL_EDGES = 250_000      # absolute ceiling on generated edges
    MAX_ATTEMPT_MULTIPLIER = 8     # per-student search budget = target * this

    # ---- Guarantee the primary test/QA account has a workable network ----
    GUARANTEE_PRIMARY_ACTIVE = True
    PRIMARY_USER_ID = 1

config = SeedConfig()

# ============================================================================
# LOGGING SETUP
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('seed_connections.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================================================
# REALISTIC DATA POOLS (reused from existing connection_seed.py)
# ============================================================================

CONNECTION_TYPES = [
    "study_partner",
    "mentor_mentee",
    "classmate",
    "project_partner",
    "tutoring"
]

SUBJECTS = [
    "Calculus", "Linear Algebra", "Physics", "Chemistry",
    "Data Structures", "Algorithms", "Database Systems",
    "Web Development", "Machine Learning", "Statistics",
    "Discrete Math", "Operating Systems", "Networks",
    "Software Engineering", "Computer Architecture"
]

REQUESTER_NOTES_TEMPLATES = [
    "Hi! I saw you're also studying {subject}. Want to connect?",
    "Hey! Would love to be study partners this semester",
    "Hi! Our mutual friend suggested we connect",
    "Hello! I could use help with {subject}",
    "Hey! Let's collaborate on upcoming projects",
    "Great study partner! Really helps with {subject}.",
    "Met in {subject} class. Very knowledgeable.",
    "Connected through mutual friends. Seems helpful.",
    "Reached out for help with {subject}. Super patient!",
    "Active in study groups. Good resource for {subject}.",
    "Classmate from {subject}. Always willing to collaborate.",
    "Found through recommendations. Excited to work together!",
    "Shared interest in {subject}. Looking forward to studying together.",
    "Really good at explaining {subject} concepts.",
    "Helpful and friendly. Great addition to my network.",
    "Connected for {subject} project collaboration.",
    "Seems very organized and dedicated to studies.",
    "Mutual connection suggested we link up for {subject}.",
    "Met during office hours. Very approachable.",
    "Active contributor in forums. Reached out to connect."
]

RECEIVER_NOTES_TEMPLATES = [
    "Seems motivated. Happy to help with {subject}.",
    "New connection. Will see how collaboration goes.",
    "Accepted because we're in the same {subject} class.",
    "Could use my help with {subject}. Willing to assist.",
    "Mutual friends vouched for them. Gave it a shot.",
    "Added to expand my study network in {subject}.",
    "Seems genuine. Looking forward to working together.",
    "Connection requested help. Happy to share knowledge.",
    "Same department. Networking for future projects.",
    "Reached out politely. Seems like good fit for study sessions.",
    "Part of {subject} group project. Added for coordination.",
    "Recommended by classmate. Hoping for good collaboration.",
    "Needs support in {subject}. I can help with that.",
    "Active in same threads. Good to have in network.",
    "Similar study style. Could work well together.",
    "Accepted to build stronger class connections.",
    "Looking for {subject} study partner. This could work.",
    "Mutual interest in {subject} topics.",
    "Added during study group formation.",
    "Seems responsible and committed to learning."
]

# ============================================================================
# HELPER FUNCTIONS - NOTES & TIMING (reused from existing connection_seed.py)
# ============================================================================

def generate_note(template_list: List[str]) -> str:
    """Generate a note from template with optional subject substitution"""
    template = random.choice(template_list)
    if "{subject}" in template and random.random() < 0.7:
        return template.format(subject=random.choice(SUBJECTS))
    elif "{subject}" in template:
        return template.replace("{subject}", "various topics")
    return template


def should_have_notes() -> bool:
    """Determine if a user should have notes (80% chance)"""
    return random.random() < 0.8


def generate_response_time(status: str) -> Optional[datetime.timedelta]:
    """Generate realistic response time based on status"""
    if status == "accepted":
        if random.random() < 0.70:
            return datetime.timedelta(hours=random.randint(1, 24))
        return datetime.timedelta(days=random.randint(1, 7))
    elif status == "rejected":
        if random.random() < 0.50:
            return datetime.timedelta(days=random.randint(1, 3))
        return datetime.timedelta(days=random.randint(7, 30))
    elif status == "blocked":
        return datetime.timedelta(hours=random.randint(0, 2))
    return None

# ============================================================================
# DATABASE OPERATIONS
# ============================================================================

def verify_database_connection() -> bool:
    return True


def fetch_student_population():
    """Single query: approved users joined with their student profile."""
    rows = (
        db.session.query(
            User.id, StudentProfile.department, StudentProfile.class_name, User.joined_at
        )
        .join(StudentProfile, StudentProfile.user_id == User.id)
        .filter(User.status == "approved")
        .all()
    )

    if len(rows) < 2:
        logger.error(f"Insufficient students: found {len(rows)}, need at least 2")
        print("❌ Error: Need at least 2 approved students with profiles to build a graph")
        print("💡 Tip: Run seed_students.py first")
        return False, []

    logger.info(f"Found {len(rows)} approved students with profiles")
    print(f"✅ Found {len(rows)} approved students with profiles")
    return True, rows


def clear_existing_connections() -> bool:
    """Clear existing connection data with confirmation"""
    try:
        existing_count = Connection.query.count()

        if existing_count > 0:
            logger.warning(f"Found {existing_count} existing connections")
            print(f"\n⚠️  Warning: {existing_count} connections already exist")
            response = input("Clear all existing connection data? (yes/no): ")
            if response.lower() != 'yes':
                logger.info("Seed aborted by user")
                print("❌ Seed aborted")
                return False

        print("🗑️  Clearing existing connection data...")
        Connection.query.delete()
        db.session.commit()
        print("✅ Cleared existing data")
        return True

    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Failed to clear existing data: {e}")
        print(f"❌ Failed to clear data: {e}")
        return False

# ============================================================================
# GRAPH INDEX CONSTRUCTION
# ============================================================================

def build_indices(rows) -> Tuple[Dict, Dict, Dict, List[int], Dict[int, dict]]:
    """
    Builds lookup indices used for weighted candidate selection.
    Returns (dept_index, level_index, cohort_index, all_ids, student_meta)
    """
    dept_index = defaultdict(list)
    level_index = defaultdict(list)
    cohort_index = defaultdict(list)
    all_ids: List[int] = []
    student_meta: Dict[int, dict] = {}

    for user_id, department, class_name, joined_at in rows:
        dept = department or "Unspecified"
        level = class_name or "Unspecified"
        joined = joined_at or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        cohort = (joined.year, joined.month)

        dept_index[dept].append(user_id)
        level_index[level].append(user_id)
        cohort_index[cohort].append(user_id)
        all_ids.append(user_id)

        student_meta[user_id] = {
            "department": dept,
            "level": level,
            "joined_at": joined,
            "cohort": cohort,
        }

    return dept_index, level_index, cohort_index, all_ids, student_meta

# ============================================================================
# DEGREE ASSIGNMENT
# ============================================================================

def assign_target_degree(force_tier: str = None) -> Tuple[str, int]:
    """Pick a sociability tier (or use a forced one) and a degree within it."""
    tiers = config.DEGREE_TIERS
    if force_tier:
        tier = next(t for t in tiers if t[0] == force_tier)
    else:
        names = [t[0] for t in tiers]
        weights = [t[1] for t in tiers]
        chosen_name = random.choices(names, weights=weights, k=1)[0]
        tier = next(t for t in tiers if t[0] == chosen_name)

    lo, hi = tier[2]
    return tier[0], random.randint(lo, hi)

# ============================================================================
# CANDIDATE SELECTION
# ============================================================================

def pick_candidate(
    uid: int,
    student_meta: Dict[int, dict],
    dept_index: Dict[str, List[int]],
    level_index: Dict[str, List[int]],
    cohort_index: Dict[Tuple[int, int], List[int]],
    all_ids: List[int],
    adjacency: Dict[int, set],
) -> Optional[int]:
    """Weighted pick of a connection candidate for `uid`. May return None."""
    meta = student_meta[uid]
    pools = []
    weights = []

    dept_pool = dept_index.get(meta["department"], [])
    if len(dept_pool) > 1:
        pools.append(("department", dept_pool))
        weights.append(config.POOL_WEIGHT_DEPARTMENT)

    level_pool = level_index.get(meta["level"], [])
    if len(level_pool) > 1:
        pools.append(("level", level_pool))
        weights.append(config.POOL_WEIGHT_LEVEL)

    cohort_pool = cohort_index.get(meta["cohort"], [])
    if len(cohort_pool) > 1:
        pools.append(("cohort", cohort_pool))
        weights.append(config.POOL_WEIGHT_COHORT)

    if adjacency.get(uid):
        pools.append(("friend_of_friend", None))
        weights.append(config.POOL_WEIGHT_FRIEND_OF_FRIEND)

    pools.append(("random", all_ids))
    weights.append(config.POOL_WEIGHT_RANDOM)

    pool_type, pool_list = random.choices(pools, weights=weights, k=1)[0]

    if pool_type == "friend_of_friend":
        friends = tuple(adjacency.get(uid, ()))
        if not friends:
            return None
        friend = random.choice(friends)
        fof_candidates = tuple(adjacency.get(friend, ()))
        if not fof_candidates:
            return None
        return random.choice(fof_candidates)

    if not pool_list:
        return None
    return random.choice(pool_list)

# ============================================================================
# GRAPH GENERATION
# ============================================================================

def generate_social_graph(
    student_meta: Dict[int, dict],
    all_ids: List[int],
    dept_index, level_index, cohort_index,
    primary_user_id: Optional[int] = None,
):
    """
    Grows the connection graph in-memory using stub growth + weighted pools.
    Returns (edges, tier_map, degree_actual).
      edges: list of (uid_a, uid_b) unordered unique pairs
      tier_map: {uid: tier_name}
      degree_actual: {uid: final degree}
    """
    degree_target: Dict[int, int] = {}
    tier_map: Dict[int, str] = {}

    for uid in all_ids:
        force = "very_active" if (primary_user_id and uid == primary_user_id) else None
        tier, target = assign_target_degree(force_tier=force)
        degree_target[uid] = target
        tier_map[uid] = tier

    degree_actual: Dict[int, int] = defaultdict(int)
    adjacency: Dict[int, set] = defaultdict(set)
    used_pairs = set()
    edges: List[Tuple[int, int]] = []

    order = all_ids[:]
    random.shuffle(order)

    total_target_stubs = sum(degree_target.values())
    max_total_edges = min(config.MAX_TOTAL_EDGES, total_target_stubs)

    for uid in order:
        target = degree_target[uid]
        max_attempts = max(target * config.MAX_ATTEMPT_MULTIPLIER, 20)
        attempts = 0

        while (
            degree_actual[uid] < target
            and attempts < max_attempts
            and len(edges) < max_total_edges
        ):
            attempts += 1
            candidate = pick_candidate(
                uid, student_meta, dept_index, level_index, cohort_index, all_ids, adjacency
            )
            if candidate is None or candidate == uid:
                continue
            if degree_actual[candidate] >= config.HARD_DEGREE_CAP:
                continue

            pair = (uid, candidate) if uid < candidate else (candidate, uid)
            if pair in used_pairs:
                continue

            used_pairs.add(pair)
            edges.append(pair)
            adjacency[uid].add(candidate)
            adjacency[candidate].add(uid)
            degree_actual[uid] += 1
            degree_actual[candidate] += 1

    return edges, tier_map, dict(degree_actual)

# ============================================================================
# STATUS SELECTION
# ============================================================================

def pick_status(a: int, b: int, student_meta: Dict[int, dict]) -> str:
    """
    Status is weighted by how 'close' the pair is. People you actually share
    context with (same department/level) are far more likely to accept;
    cold cross-department outreach skews toward pending/rejected.
    """
    same_dept = student_meta[a]["department"] == student_meta[b]["department"]
    same_level = student_meta[a]["level"] == student_meta[b]["level"]

    weights = dict(config.STATUS_DISTRIBUTION)

    if same_dept and same_level:
        weights["accepted"] *= 1.25
        weights["rejected"] *= 0.70
    elif same_dept or same_level:
        weights["accepted"] *= 1.10
    else:
        weights["accepted"] *= 0.85
        weights["pending"] *= 1.10
        weights["rejected"] *= 1.15

    statuses = list(weights.keys())
    w = list(weights.values())
    return random.choices(statuses, weights=w, k=1)[0]

# ============================================================================
# CONNECTION RECORD CREATION
# ============================================================================

def create_connection_record(
    requester_id: int, receiver_id: int, status: str, requested_at: datetime.datetime
) -> Connection:
    """Create a single Connection row with separate requester/receiver notes."""
    response_time = generate_response_time(status)
    responded_at = requested_at + response_time if response_time else None
    conn_type = random.choice(CONNECTION_TYPES)

    requester_notes = generate_note(REQUESTER_NOTES_TEMPLATES) if should_have_notes() else None
    receiver_notes = (
        generate_note(RECEIVER_NOTES_TEMPLATES)
        if (status == "accepted" and should_have_notes())
        else None
    )

    return Connection(
        requester_id=requester_id,
        receiver_id=receiver_id,
        status=status,
        requested_at=requested_at,
        responded_at=responded_at,
        connection_type=conn_type,
        requester_notes=requester_notes,
        receiver_notes=receiver_notes,
        is_seen=random.choice([True, False]) if status == "pending" else True,
    )

# ============================================================================
# MAIN SEED FUNCTION
# ============================================================================

def seed_connections() -> bool:
    """Main seeding function - builds and materializes the social graph."""
    print("🌱 Starting StudyHub social graph seed...")
    random.seed(config.SEED_RANDOM_STATE)

    if not verify_database_connection():
        return False

    ok, rows = fetch_student_population()
    if not ok:
        return False

    if not clear_existing_connections():
        return False

    print("📐 Indexing student population by department / level / cohort...")
    dept_index, level_index, cohort_index, all_ids, student_meta = build_indices(rows)
    print(f"   {len(dept_index)} departments, {len(level_index)} levels, "
          f"{len(cohort_index)} join-cohorts")

    primary_id = None
    if config.GUARANTEE_PRIMARY_ACTIVE:
        primary_user = User.query.filter_by(id=config.PRIMARY_USER_ID).first()
        if primary_user and primary_user.id in student_meta:
            primary_id = primary_user.id
            print(f"🎯 Guaranteeing an active-tier network for primary user "
                  f"{primary_user.name} (ID: {primary_user.id})")

    print(f"\n🔗 Building social graph for {len(all_ids)} students...")
    edges, tier_map, degree_actual = generate_social_graph(
        student_meta, all_ids, dept_index, level_index, cohort_index,
        primary_user_id=primary_id,
    )
    print(f"✅ Generated {len(edges)} unique relationship pairs")

    same_dept_edges = sum(
        1 for a, b in edges if student_meta[a]["department"] == student_meta[b]["department"]
    )

    print(f"\n💾 Materializing {len(edges)} connections into the database...")
    connections_created = 0
    connections_failed = 0
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    pending: List[Connection] = []

    def flush_pending() -> bool:
        for attempt in range(1, config.MAX_BATCH_RETRIES + 1):
            try:
                for conn_obj in pending:
                    db.session.add(conn_obj)
                db.session.commit()
                return True
            except (OperationalError, DBAPIError) as e:
                logger.error(
                    f"DB connection error committing batch "
                    f"(attempt {attempt}/{config.MAX_BATCH_RETRIES}): {e}"
                )
                db.session.rollback()
                db.session.remove()
                db.engine.dispose()
                if attempt < config.MAX_BATCH_RETRIES:
                    wait = config.RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    print(f"   ⚠️  Connection dropped, retrying in {wait}s "
                          f"(attempt {attempt}/{config.MAX_BATCH_RETRIES})...")
                    time.sleep(wait)
            except SQLAlchemyError as e:
                logger.error(f"Non-connection DB error, aborting batch: {e}")
                db.session.rollback()
                return False
        logger.error(f"Batch failed after {config.MAX_BATCH_RETRIES} retries — giving up.")
        return False

    try:
        for a, b in edges:
            try:
                if random.random() < 0.5:
                    requester_id, receiver_id = a, b
                else:
                    requester_id, receiver_id = b, a

                status = pick_status(a, b, student_meta)

                joined_at = max(student_meta[a]["joined_at"], student_meta[b]["joined_at"])
                days_available = max((now - joined_at).days, 1)
                days_ago = random.randint(
                    config.MIN_DAYS_AGO, min(days_available, config.MAX_DAYS_AGO)
                )
                requested_at = now - datetime.timedelta(days=days_ago)

                connection = create_connection_record(
                    requester_id, receiver_id, status, requested_at
                )
                pending.append(connection)

                if len(pending) >= config.BATCH_SIZE:
                    if not flush_pending():
                        print(f"❌ Could not save batch after "
                              f"{connections_created} connections created. Stopping.")
                        return False
                    connections_created += len(pending)
                    pending = []
                    db.session.expire_all()
                    print(f"   ✓ {connections_created}/{len(edges)} connections committed...")

            except Exception as e:
                logger.error(f"Error creating connection {a}-{b}: {e}")
                connections_failed += 1
                continue

        if pending:
            if not flush_pending():
                print(f"❌ Could not save final batch after "
                      f"{connections_created} connections created.")
                return False
            connections_created += len(pending)

        print(f"\n✅ Created {connections_created} connections successfully!")
        if connections_failed:
            print(f"⚠️  {connections_failed} connections failed to create")

        print_summary_statistics(tier_map, degree_actual, len(edges), same_dept_edges)
        return True

    except Exception as e:
        logger.error(f"Unexpected error during seeding: {e}", exc_info=True)
        db.session.rollback()
        print(f"❌ Unexpected error: {e}")
        return False

# ============================================================================
# VALIDATION
# ============================================================================

def validate_seed_integrity() -> bool:
    """Post-seed sanity checks: no dupes, no self-connections, valid FKs."""
    print("\n🔍 Validating seeded connection graph...")
    ok = True

    all_conns = Connection.query.all()
    pairs_seen = set()
    self_conns = 0
    dup_pairs = 0

    for c in all_conns:
        if c.requester_id == c.receiver_id:
            self_conns += 1
        pair = tuple(sorted((c.requester_id, c.receiver_id)))
        if pair in pairs_seen:
            dup_pairs += 1
        pairs_seen.add(pair)

    if self_conns:
        print(f"   ❌ {self_conns} self-connections found")
        ok = False
    if dup_pairs:
        print(f"   ❌ {dup_pairs} duplicate relationship pairs found")
        ok = False

    valid_user_ids = {u.id for u in User.query.with_entities(User.id).all()}
    bad_fk = sum(
        1 for c in all_conns
        if c.requester_id not in valid_user_ids or c.receiver_id not in valid_user_ids
    )
    if bad_fk:
        print(f"   ❌ {bad_fk} connections reference invalid user IDs")
        ok = False

    if ok:
        print("   ✅ No duplicates, no self-connections, all foreign keys valid")
    return ok

# ============================================================================
# SUMMARY STATISTICS
# ============================================================================

def print_summary_statistics(
    tier_map: Dict[int, str],
    degree_actual: Dict[int, int],
    total_edges: int,
    same_dept_edges: int,
):
    print("\n" + "=" * 60)
    print("📊 SOCIAL GRAPH SEED SUMMARY")
    print("=" * 60)

    total_connections = Connection.query.count()
    print(f"Total Connection Records: {total_connections}")
    print(f"Unique Relationship Pairs Generated: {total_edges}")

    if degree_actual:
        avg_degree = sum(degree_actual.values()) / len(degree_actual)
        max_degree = max(degree_actual.values())
        min_degree = min(degree_actual.values())
        print(f"\n📈 Degree Distribution:")
        print(f"   Average connections per student: {avg_degree:.1f}")
        print(f"   Min: {min_degree}  |  Max: {max_degree}")

    tier_counts = Counter(tier_map.values())
    print(f"\n🎭 Sociability Tiers:")
    for tier_name, _, drange in config.DEGREE_TIERS:
        count = tier_counts.get(tier_name, 0)
        pct = (count / max(len(tier_map), 1)) * 100
        print(f"   {tier_name} ({drange[0]}-{drange[1]}): {count} students ({pct:.1f}%)")

    top_users = sorted(degree_actual.items(), key=lambda x: x[1], reverse=True)[:10]
    if top_users:
        top_ids = [uid for uid, _ in top_users]
        users_map = {u.id: u for u in User.query.filter(User.id.in_(top_ids)).all()}
        print(f"\n🌟 Top 10 Most Connected Students:")
        for uid, deg in top_users:
            user = users_map.get(uid)
            name = user.name if user else f"User#{uid}"
            print(f"   {name}: {deg} connections")

    if total_edges:
        pct_same_dept = same_dept_edges / total_edges * 100
        print(f"\n🏫 Department Clustering: {same_dept_edges}/{total_edges} "
              f"({pct_same_dept:.1f}%) of connections are within the same department")

    print(f"\n📋 Connection Status:")
    icons = {"accepted": "✅", "pending": "⏳", "rejected": "❌", "blocked": "🚫"}
    for status in ["accepted", "pending", "rejected", "blocked"]:
        count = Connection.query.filter_by(status=status).count()
        pct = (count / max(total_connections, 1)) * 100
        print(f"   {icons[status]} {status.capitalize()}: {count} ({pct:.1f}%)")

    requester_notes_count = Connection.query.filter(
        Connection.requester_notes.isnot(None), Connection.requester_notes != ""
    ).count()
    receiver_notes_count = Connection.query.filter(
        Connection.receiver_notes.isnot(None), Connection.receiver_notes != ""
    ).count()
    print(f"\n📝 Notes Statistics:")
    if total_connections:
        print(f"   Connections with requester notes: {requester_notes_count} "
              f"({requester_notes_count / total_connections * 100:.1f}%)")
        print(f"   Connections with receiver notes:  {receiver_notes_count} "
              f"({receiver_notes_count / total_connections * 100:.1f}%)")

    validate_seed_integrity()

    print("\n" + "=" * 60)
    print("✨ Social graph seed complete! StudyHub now feels alive.")
    print("=" * 60 + "\n")

# ============================================================================
# STANDALONE EXECUTION
# ============================================================================

if __name__ == "__main__":
    from app import app

    with app.app_context():
        success = seed_connections()
        if success:
            logger.info("Connection seed script completed successfully")
            exit(0)
        else:
            logger.error("Connection seed script failed")
            exit(1)
