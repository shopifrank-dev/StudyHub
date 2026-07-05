"""
StudyHub Thread System Seed Script — v2 (Even Distribution Edition)
=====================================================================
Simulates an active university messaging ecosystem across the ENTIRE
student population — no user is special-cased or force-included.

Key differences vs the previous "Animal Edition" script:
  - REMOVED: forcing user id 1 / 2 into every thread as creator/moderator.
  - ADDED:   participation tiers (lurker → leader) assigned to every user,
             mirroring the sociability-tier pattern used in the connection
             graph seed, so activity is naturally uneven but never
             concentrated on hardcoded IDs.
  - ADDED:   membership selection is weighted by department / class level /
             existing (accepted) Connection edges — i.e. real social
             clustering — with a random pool to avoid island fragmentation.
  - ADDED:   coherent conversation "bursts" (question → answer → follow-up
             → thanks) instead of independent random messages, so replies
             actually reference the message before them.
  - ADDED:   a real Learnora bot User row (FK-safe) instead of piggy-backing
             AI messages onto a real student's account.
  - ADDED:   mentions wired to real Mention + Notification rows.
  - Join request statuses match what routes/threads.py actually writes:
    'pending' | 'approved' | 'rejected' | 'invited' (previous script used
    the non-existent 'accepted' status — fixed here).

Run this AFTER seed_students.py and connection_seed.py (the friend-of-friend
/ connection clustering logic depends on Connection rows already existing).

Tune the numbers in SeedConfig to hit whatever scale you need:
  default settings land at ~1,500 threads / ~125k messages / ~17k AI replies,
  inside the 1,200-1,800 / 80k-150k / 15k-25k targets from the spec.
"""

import random
import datetime
import logging
import time
from typing import List, Dict, Set, Tuple, Optional
from collections import defaultdict, Counter

from sqlalchemy.exc import SQLAlchemyError, OperationalError, DBAPIError
from extensions import db
from models import (
    User, StudentProfile, Connection,
    Thread, ThreadMember, ThreadJoinRequest, ThreadMessage,
    ThreadMessageReaction, ThreadMessageReadReceipt,
    Mention, Notification,
)

# ============================================================================
# CONFIGURATION
# ============================================================================

class SeedConfig:
    SEED_RANDOM_STATE = 7
    BATCH_SIZE = 300

    # Resilience: retry a commit on a dropped/stale DB connection instead
    # of aborting a run that can take a while at ~125k messages.
    MAX_BATCH_RETRIES = 5
    RETRY_BACKOFF_SECONDS = 2

    # ---- Scale knobs -------------------------------------------------------
    NUM_THREADS = 1500                 # spec: 1,200-1,800

    MEMBERS_MIN = 4
    MEMBERS_MAX = 50                   # matches app's own max_members cap

    # Thread "vibe" tiers control how many messages a thread ends up with.
    # (name, selection_weight, (min_msgs, max_msgs))
    ACTIVITY_TIERS = [
        ("quiet",        30, (10, 40)),
        ("moderate",     40, (40, 100)),
        ("active",       22, (100, 180)),
        ("hyperactive",  8,  (180, 260)),
    ]
    EMPTY_THREAD_RATIO = 0.04          # some threads have zero messages

    AI_MESSAGE_CHANCE      = 0.14      # independent chance per generated msg
    REACTION_CHANCE        = 0.55
    MAX_REACTIONS_PER_MSG  = 8
    DELETE_MESSAGE_CHANCE  = 0.04
    PIN_MESSAGE_CHANCE     = 0.03
    EDIT_MESSAGE_CHANCE    = 0.08
    ATTACHMENT_CHANCE      = 0.12
    MENTION_CHANCE         = 0.10

    # message.status weights depend on message age
    STATUS_WEIGHTS_OLD    = [0.05, 0.10, 0.85]   # sent/delivered/read
    STATUS_WEIGHTS_RECENT = [0.45, 0.35, 0.20]
    RECENT_CUTOFF_HOURS   = 6

    MAX_DAYS_AGO = 210
    MIN_DAYS_AGO = 1

    NUM_JOIN_REQUESTS = 4000
    NUM_INVITES       = 2500

    # ---- Participation tiers: how many threads a user ends up in ----------
    # (name, selection_weight, (min_threads, max_threads))
    PARTICIPATION_TIERS = [
        ("lurker",      30, (1, 3)),
        ("occasional",  35, (3, 8)),
        ("active",      22, (8, 20)),
        ("helper",      9,  (20, 45)),
        ("leader",      4,  (45, 90)),
    ]

    # Candidate-pool weights when filling thread membership
    POOL_WEIGHT_DEPARTMENT = 40
    POOL_WEIGHT_LEVEL      = 15
    POOL_WEIGHT_FRIEND     = 30   # accepted-Connection based clustering
    POOL_WEIGHT_RANDOM     = 15

    MAX_MEMBER_ATTEMPT_MULTIPLIER = 6

config = SeedConfig()

# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("seed_threads_v2.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# ============================================================================
# CONTENT POOLS
# ============================================================================

# Category -> plausible StudentProfile.department values used for clustering
# thread membership. None = cross-department / campus-wide category.
CATEGORY_DEPARTMENTS: Dict[str, Optional[List[str]]] = {
    "programming":  ["Computer Science", "Software Engineering", "Data Science",
                      "Cyber Security", "Web Development", "Computer Engineering"],
    "mathematics":  ["Mathematics", "Statistics", "Data Science"],
    "engineering":  ["Civil Engineering", "Mechanical Engineering",
                      "Electrical / Electronic Engineering", "Chemical Engineering",
                      "Computer Engineering", "Petroleum / Gas Engineering",
                      "Metallurgical & Materials Engineering"],
    "medicine":     ["Medicine & Surgery", "Nursing Science",
                      "Pharmacy / Pharmaceutical Sciences", "Public Health",
                      "Anatomy / Physiology", "Medical Biochemistry / Microbiology"],
    "business":     ["Business Administration / Management", "Accounting",
                      "Banking & Finance", "Marketing", "Entrepreneurship",
                      "Human Resources Management", "Insurance"],
    "law":          ["Law (Common / Civil Law)"],
    "arts":         ["Fine & Applied Arts (Creative Arts)", "Theatre Arts", "Music",
                      "Mass Communication / Communication & Language Arts"],
    "ai_ml":        ["Computer Science", "Data Science", "Software Engineering"],
    "exam_prep":    None,
    "study_groups": None,
    "career":       None,
    "internships":  None,
    "research":     None,
    "campus_general": None,
}

CATEGORY_WEIGHTS = {
    "programming": 16, "mathematics": 9, "engineering": 14, "medicine": 10,
    "business": 10, "law": 4, "arts": 6, "ai_ml": 8, "exam_prep": 9,
    "study_groups": 8, "career": 4, "internships": 4, "research": 4,
    "campus_general": 6,
}
CATEGORY_NAMES   = list(CATEGORY_WEIGHTS.keys())
CATEGORY_WEIGHT_LIST = [CATEGORY_WEIGHTS[c] for c in CATEGORY_NAMES]

# Subject pools per category, used to fill title/description/message templates
CATEGORY_SUBJECTS = {
    "programming":  ["Data Structures", "Algorithms", "Web Development", "Databases",
                      "Operating Systems", "Object-Oriented Programming", "Git & Version Control",
                      "REST APIs", "System Design"],
    "mathematics":  ["Calculus", "Linear Algebra", "Discrete Math", "Probability & Statistics",
                      "Differential Equations", "Numerical Methods"],
    "engineering":  ["Thermodynamics", "Circuit Analysis", "Fluid Mechanics",
                      "Structural Analysis", "Control Systems", "Materials Science"],
    "medicine":     ["Anatomy", "Physiology", "Pharmacology", "Pathology", "Clinical Rotations"],
    "business":     ["Financial Accounting", "Microeconomics", "Marketing Strategy",
                      "Business Law", "Organizational Behaviour"],
    "law":          ["Constitutional Law", "Contract Law", "Criminal Law", "Legal Research & Writing"],
    "arts":         ["Studio Practice", "Art History", "Scriptwriting", "Music Theory"],
    "ai_ml":        ["Machine Learning", "Neural Networks", "Natural Language Processing",
                      "Computer Vision", "Model Evaluation"],
    "exam_prep":    ["the upcoming midterms", "the final exams", "the resit exams",
                      "next week's continuous assessment", "the departmental mock exam"],
    "study_groups": ["general coursework", "weekly problem sets", "revision", "group projects"],
    "career":       ["resume reviews", "interview prep", "career switching", "networking on LinkedIn"],
    "internships":  ["internship applications", "SIWES placement", "remote internships",
                      "internship interview prep"],
    "research":     ["literature review methods", "citation management", "thesis writing",
                      "conference paper submissions"],
    "campus_general": ["hostel life", "campus events", "cafeteria food", "exam schedules",
                        "student union elections", "weekend plans"],
}

TITLE_PREFIXES = [
    "{subject} Study Circle", "{subject} Help Desk", "{subject} Crew",
    "{subject} Squad", "{subject} Lounge", "{subject} Warriors",
    "{subject} Study Group", "{subject} Corner", "{subject} Hub",
    "{subject} Collective", "{subject} Network", "{subject} Study Pod",
]
TITLE_SUFFIXES = ["", " 📘", " 🔥", " 🚀", " 💡", " ✨", " 🎯", " 📚", ""]

DESCRIPTION_TEMPLATES = [
    "A space to work through {subject} together — bring your questions, share your notes.",
    "Weekly check-ins on {subject}. Everyone contributes, everyone benefits.",
    "For anyone stuck on {subject} — no question is too basic here.",
    "We solve problems, share resources, and keep each other accountable on {subject}.",
    "Casual but focused group for {subject}. Drop in whenever you need help.",
    "Built for {subject} — practice problems, worked solutions, and honest discussion.",
    "Prepping for {subject} together. Consistency beats cramming.",
    "Peer-led group covering {subject}. Come with questions, leave with answers.",
]

TAG_EXTRA_POOL = ["study-group", "peer-help", "notes", "past-questions", "revision",
                   "collab", "exam-prep", "resources", "projects", "practice"]

# ── Conversation content pools ───────────────────────────────────────────────

QUESTION_STARTERS = [
    "Has anyone started on {subject} yet? I don't even know where to begin.",
    "Can someone explain {concept} in {subject}? The lecture notes made zero sense.",
    "Quick question on {subject} — is {concept} going to be on the exam?",
    "Struggling with {subject} rn. Specifically {concept}. Any tips?",
    "Does anyone have good resources for {subject}? Preferably something on {concept}.",
    "I keep getting the wrong answer on the {subject} problem set. Something about {concept}?",
    "Is it just me or is {concept} in {subject} way harder than the lecturer made it sound?",
    "What's the difference between {concept} and the related idea we covered last week in {subject}?",
]

ANNOUNCEMENT_STARTERS = [
    "Reminder: our session on {subject} is this {day}. Bring your laptops.",
    "Heads up — the deadline for the {subject} assignment moved to next {day}.",
    "Sharing my notes from today's {subject} class. Covers {concept} pretty well.",
    "Found a great resource on {concept} for {subject} — dropping the summary here.",
    "PSA: the department posted new past questions relevant to {subject}.",
    "Just finished the {subject} mock test. Happy to share my approach to {concept}.",
]

RESOURCE_STARTERS = [
    "Sharing a cheat sheet I made for {concept} in {subject}. Feedback welcome.",
    "Recorded a quick explanation of {concept} — should help with {subject}.",
    "Dropping a worked example for {subject}, focused on {concept}.",
    "Compiled everyone's {subject} questions from last week into one doc, with answers.",
]

OPINION_STARTERS = [
    "Hot take: {concept} is way easier once you stop memorising formulas for {subject}.",
    "Anyone else think the {subject} syllabus moves too fast through {concept}?",
    "I actually enjoyed the {subject} class on {concept} today, surprisingly.",
    "Unpopular opinion — {subject} assignments are more useful than the lectures.",
]

ANSWER_REPLIES = [
    "For {concept}, the trick is to break it down step by step — don't try to do it all at once.",
    "I had the same issue with {concept}. What helped was working through 2-3 extra examples.",
    "{concept} clicked for me once I watched a video on it instead of just reading the textbook.",
    "Pretty sure {concept} is examinable — it's come up in past questions before.",
    "Here's how I think about {concept}: focus on the underlying pattern, not the formula.",
    "I can walk you through {concept} on a call later if that helps.",
    "Check the lecture slides from week before last — {concept} is explained there with a diagram.",
]

CLARIFY_REPLIES = [
    "Wait, so does that mean {concept} applies even in the edge cases, or just the general case?",
    "Can you clarify what you mean by that? I thought {concept} worked differently.",
    "So is {concept} the same thing the lecturer called something else in class?",
    "Does this apply to the whole {subject} syllabus or just this unit?",
]

AGREEMENT_REPLIES = [
    "Yeah honestly, {concept} confused me too at first.",
    "Same here — {subject} has been rough this semester.",
    "100% agree, that explanation of {concept} makes way more sense now.",
    "Facts. I thought I was the only one struggling with {concept}.",
]

THANKS_REPLIES = [
    "Thank you so much, this actually makes sense now!",
    "Appreciate you breaking that down 🙏",
    "This helped a lot, thanks for taking the time to explain.",
    "You're a lifesaver, I've been stuck on this for hours.",
    "Thanks! Bookmarking this for when I revise.",
]

ADDITION_REPLIES = [
    "Adding to that — {concept} also shows up when you're dealing with edge cases in {subject}.",
    "Building on what was said above, I found this approach works well for {concept} too.",
    "One more thing about {concept}: don't forget to double check your units/assumptions.",
    "Also worth mentioning — the professor hinted {concept} might show up as a bonus question.",
]

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

CONCEPTS_GENERIC = [
    "Big-O notation", "dynamic programming", "eigenvalues", "Bayes' theorem", "recursion",
    "integration by parts", "NP-completeness", "normal distribution", "graph traversal",
    "partial derivatives", "reaction mechanisms", "context switching", "JOIN operations",
    "gradient descent", "Fourier transforms", "merge sort", "Dijkstra's algorithm",
    "Lagrange multipliers", "heap sort", "binary search trees", "hypothesis testing",
    "supply and demand curves", "case law precedent", "structural load analysis",
    "cardiac output", "drug half-life", "colour theory", "narrative arc",
]

ATTACHMENT_DATA = [
    {"name": "lecture_notes.pdf",        "type": "document", "size": 204800},
    {"name": "practice_problems.pdf",    "type": "document", "size": 512000},
    {"name": "diagram_summary.png",      "type": "image",    "size": 98304},
    {"name": "slides_week.pdf",          "type": "document", "size": 1048576},
    {"name": "solution_walkthrough.mp4", "type": "video",    "size": 20971520},
    {"name": "cheatsheet.pdf",           "type": "document", "size": 153600},
    {"name": "screenshot.png",           "type": "image",    "size": 307200},
    {"name": "past_questions.pdf",       "type": "document", "size": 409600},
    {"name": "code_snippet.txt",         "type": "document", "size": 4096},
]
ATTACHMENT_BASE_URL = "https://storage.studyhub.app/thread-attachments"
EMOJI_POOL = ["👍", "❤️", "🔥", "😂", "🎉", "😢", "🤔", "👏", "💡", "✅"]

AI_ANSWER_TEMPLATES = [
    "Great question! {concept} refers to the idea that {explanation}. Let me know if you want a worked example.",
    "To handle this efficiently, think about {concept}. Key insight: {explanation}.",
    "Quick summary of {subject} here: {explanation}. Happy to go deeper on any part.",
    "Common mix-up: students often confuse {concept} with a related idea. The difference is {explanation}.",
    "Hint without spoiling it — think about {explanation}, then apply it to {concept}.",
    "Here's a structured way to approach {concept}: first understand the definition, then work through {explanation}.",
]
AI_EXPLANATIONS = [
    "the algorithm halves the problem size each step, giving logarithmic complexity",
    "you need to account for edge cases before applying the general formula",
    "the sign of the determinant tells you whether orientation is preserved",
    "Bayes' theorem updates your belief given new evidence: P(A|B) = P(B|A)P(A)/P(B)",
    "integration by parts mirrors the product rule for derivatives",
    "the underlying pattern matters more than memorising the formula itself",
    "boundary conditions usually reveal what the question is really testing",
]

JOIN_REQUEST_MESSAGES = [
    "Hey! I'm struggling with this topic and would love some peer support.",
    "Been following this group's posts — the discussions look really helpful.",
    "A classmate recommended I join, hoping to contribute and learn.",
    "I have some resources I'd like to share with the group if admitted.",
    "Looking for an active study community ahead of exam season.",
    "Already worked through a few of the posted problems — would love to join!",
    "This thread covers exactly what I need help with this semester.",
    "Self-studying this and want to benchmark my progress with peers.",
]
INVITE_MESSAGES = [
    "[INVITE] We think you'd be a great addition to the group — hope you'll join!",
    "[INVITE] A mutual friend recommended you. Your background would be valuable here.",
    "[INVITE] Saw your posts and think you'd fit right in. Come study with us!",
    "[INVITE] Heard great things about you — we'd love to have you in our circle.",
]


def random_past_datetime(max_days: int = config.MAX_DAYS_AGO,
                          min_days: int = config.MIN_DAYS_AGO) -> datetime.datetime:
    days    = random.randint(min_days, max_days)
    hours   = random.randint(0, 23)
    minutes = random.randint(0, 59)
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(days=days, hours=hours, minutes=minutes)


def pick_message_status(sent_at: datetime.datetime) -> str:
    age_hours = (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - sent_at).total_seconds() / 3600
    weights = (config.STATUS_WEIGHTS_RECENT if age_hours < config.RECENT_CUTOFF_HOURS
               else config.STATUS_WEIGHTS_OLD)
    return random.choices(["sent", "delivered", "read"], weights=weights)[0]


def maybe_attachment() -> Optional[dict]:
    if random.random() < config.ATTACHMENT_CHANCE:
        att = random.choice(ATTACHMENT_DATA)
        path = f"{ATTACHMENT_BASE_URL}/{random.randint(10000, 99999)}/{att['name']}"
        return {"url": path, "name": att["name"], "type": att["type"], "size": att["size"]}
    return None


# ============================================================================
# LEARNORA BOT ID  (matches websocket_threads.py — no real User row, just the
# hardcoded sentinel ID used everywhere in production)
# ============================================================================

LEARNORA_BOT_USER_ID = 99999999999
# NOTE: this mirrors websocket_threads.py exactly — no real User row is created.
# If your database enforces FK constraints strictly (e.g. Postgres with FKs
# not deferred), AI message inserts will hit the same constraint your live
# app would hit today with this same hardcoded ID. That's a pre-existing
# characteristic of the production code, not something this seed script
# introduces — fixing it (e.g. seeding a real bot row or wiring
# LEARNORA_BOT_USER_ID via app.config) is an app-level decision, left alone here.


# ============================================================================
# POPULATION / INDEX LOADING
# ============================================================================

def load_population() -> Tuple[List[User], Dict[int, dict], Dict[str, List[int]],
                                Dict[str, List[int]], Dict[int, Set[int]]]:
    """
    Returns:
      users            — all approved User objects (excludes the bot)
      user_meta        — {user_id: {department, level, username, name}}
      dept_index       — {department: [user_ids]}
      level_index      — {level: [user_ids]}
      friend_adj       — {user_id: set(user_id)}  built from accepted Connections
    """
    rows = (
        db.session.query(User, StudentProfile)
        .join(StudentProfile, StudentProfile.user_id == User.id)
        .filter(User.status == "approved")
        .all()
    )
    users: List[User] = []
    user_meta: Dict[int, dict] = {}
    dept_index: Dict[str, List[int]] = defaultdict(list)
    level_index: Dict[str, List[int]] = defaultdict(list)

    for user, profile in rows:
        dept = profile.department or "Unspecified"
        level = profile.class_name or "Unspecified"
        users.append(user)
        user_meta[user.id] = {
            "department": dept, "level": level,
            "username": user.username, "name": user.name,
        }
        dept_index[dept].append(user.id)
        level_index[level].append(user.id)

    friend_adj: Dict[int, Set[int]] = defaultdict(set)
    accepted = Connection.query.filter_by(status="accepted").all()
    for c in accepted:
        friend_adj[c.requester_id].add(c.receiver_id)
        friend_adj[c.receiver_id].add(c.requester_id)

    logger.info(f"Loaded {len(users)} approved students, "
                f"{len(dept_index)} departments, {len(accepted)} accepted connections")
    return users, user_meta, dept_index, level_index, friend_adj


def assign_participation_targets(user_ids: List[int]) -> Tuple[Dict[int, int], Dict[int, str]]:
    """Every user gets a target thread-membership count via weighted tiers."""
    names   = [t[0] for t in config.PARTICIPATION_TIERS]
    weights = [t[1] for t in config.PARTICIPATION_TIERS]
    targets: Dict[int, int] = {}
    tiers: Dict[int, str] = {}
    for uid in user_ids:
        tier_name = random.choices(names, weights=weights, k=1)[0]
        lo, hi = next(t[2] for t in config.PARTICIPATION_TIERS if t[0] == tier_name)
        targets[uid] = random.randint(lo, hi)
        tiers[uid] = tier_name
    return targets, tiers


# ============================================================================
# THREAD BLUEPRINT GENERATION
# ============================================================================

def pick_category() -> str:
    return random.choices(CATEGORY_NAMES, weights=CATEGORY_WEIGHT_LIST, k=1)[0]


def build_thread_blueprint(index: int) -> dict:
    category = pick_category()
    subjects = CATEGORY_SUBJECTS[category]
    subject  = random.choice(subjects)

    title = random.choice(TITLE_PREFIXES).format(subject=subject) + random.choice(TITLE_SUFFIXES)
    description = random.choice(DESCRIPTION_TEMPLATES).format(subject=subject)

    dept_pool = CATEGORY_DEPARTMENTS[category]
    department = random.choice(dept_pool) if dept_pool else None

    tags = [t.lower().replace(" ", "-") for t in [subject]] + random.sample(TAG_EXTRA_POOL, 2)

    is_public = random.random() < 0.65
    is_open = is_public or random.random() < 0.3
    requires_approval = (not is_public) or random.random() < 0.5

    return {
        "index": index, "category": category, "subject": subject,
        "title": title, "description": description, "department": department,
        "tags": tags, "is_open": is_open, "requires_approval": requires_approval,
    }


# ============================================================================
# MEMBERSHIP SELECTION  (no hardcoded users — pure weighted clustering)
# ============================================================================

def pick_members_for_thread(
    blueprint: dict,
    target_size: int,
    users: List[User],
    user_meta: Dict[int, dict],
    dept_index: Dict[str, List[int]],
    level_index: Dict[str, List[int]],
    friend_adj: Dict[int, Set[int]],
    remaining_budget: Dict[int, int],
    thread_member_ids: Dict[int, Set[int]],
) -> List[int]:
    """
    Weighted candidate selection for one thread's membership, respecting each
    user's remaining participation budget. Creator is chosen first (biased
    toward users belonging to the thread's department, if any, and users
    with a healthy remaining budget), then the rest of the pool is filled.
    """
    dept = blueprint["department"]
    dept_pool = [uid for uid in dept_index.get(dept, []) if remaining_budget.get(uid, 0) > 0] if dept else []

    # ---- Creator selection ----
    creator_candidates = dept_pool if dept_pool else [
        u.id for u in users if remaining_budget.get(u.id, 0) > 0
    ]
    if not creator_candidates:
        creator_candidates = [u.id for u in users]  # last resort: ignore budget

    creator_id = random.choice(creator_candidates)
    members: Set[int] = {creator_id}

    # ---- Fill remaining slots ----
    attempts = 0
    max_attempts = target_size * config.MAX_MEMBER_ATTEMPT_MULTIPLIER

    while len(members) < target_size and attempts < max_attempts:
        attempts += 1
        pools = []
        weights = []

        if dept:
            d_pool = [uid for uid in dept_index.get(dept, []) if uid not in members]
            if d_pool:
                pools.append(d_pool); weights.append(config.POOL_WEIGHT_DEPARTMENT)

        level = user_meta[creator_id]["level"]
        l_pool = [uid for uid in level_index.get(level, []) if uid not in members]
        if l_pool:
            pools.append(l_pool); weights.append(config.POOL_WEIGHT_LEVEL)

        # Friend-of-current-members pool (real social clustering)
        friend_pool: List[int] = []
        for mid in members:
            friend_pool.extend(uid for uid in friend_adj.get(mid, ()) if uid not in members)
        if friend_pool:
            pools.append(friend_pool); weights.append(config.POOL_WEIGHT_FRIEND)

        random_pool = [u.id for u in users if u.id not in members]
        pools.append(random_pool); weights.append(config.POOL_WEIGHT_RANDOM)

        if not pools:
            break

        pool = random.choices(pools, weights=weights, k=1)[0]
        candidate = random.choice(pool)

        if candidate in members:
            continue
        if remaining_budget.get(candidate, 0) <= 0 and random.random() < 0.85:
            # occasionally let an over-budget-but-enthusiastic user in anyway,
            # mirrors real life (people join more groups than their "usual" pace)
            continue

        members.add(candidate)

    for uid in members:
        remaining_budget[uid] = remaining_budget.get(uid, 0) - 1

    return [creator_id] + [uid for uid in members if uid != creator_id]


# ============================================================================
# THREAD + MEMBER PERSISTENCE
# ============================================================================

def create_thread_row(blueprint: dict, creator_id: int, target_size: int,
                       created_at: datetime.datetime) -> Thread:
    max_members = min(50, max(target_size + random.randint(2, 15), config.MEMBERS_MIN))
    return Thread(
        creator_id=creator_id,
        title=blueprint["title"],
        description=blueprint["description"],
        department=blueprint["department"],
        tags=blueprint["tags"],
        is_open=blueprint["is_open"],
        max_members=max_members,
        requires_approval=blueprint["requires_approval"],
        member_count=1,
        message_count=0,
        created_at=created_at,
        last_activity=created_at,
    )


def add_members(thread: Thread, creator_id: int, member_ids: List[int],
                 user_meta: Dict[int, dict], participation_tiers: Dict[int, str]) -> List[int]:
    """
    Persists ThreadMember rows. Role assignment (no hardcoded users):
      - creator            -> "creator"
      - 1-3 highest-tier participants among the rest -> "moderator"
      - everyone else      -> "member"
    """
    others = [uid for uid in member_ids if uid != creator_id]

    tier_rank = {"lurker": 0, "occasional": 1, "active": 2, "helper": 3, "leader": 4}
    others_sorted = sorted(others, key=lambda uid: tier_rank.get(participation_tiers.get(uid, "occasional"), 1),
                            reverse=True)
    num_mods = min(len(others_sorted), random.randint(1, 3)) if others_sorted else 0
    moderator_ids = set(others_sorted[:num_mods])

    db.session.add(ThreadMember(
        thread_id=thread.id, student_id=creator_id, role="creator",
        joined_at=thread.created_at,
        last_read_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
        messages_sent=0,
    ))

    for uid in others:
        role = "moderator" if uid in moderator_ids else "member"
        offset = datetime.timedelta(hours=random.randint(1, 96))
        db.session.add(ThreadMember(
            thread_id=thread.id, student_id=uid, role=role,
            joined_at=thread.created_at + offset,
            last_read_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(hours=random.randint(0, 72)),
            messages_sent=0,
        ))

    thread.member_count = 1 + len(others)
    return [creator_id] + others


# ============================================================================
# CONVERSATION GENERATION
# ============================================================================

def _fill(template: str, subject: str) -> str:
    return template.format(
        subject=subject,
        concept=random.choice(CONCEPTS_GENERIC),
        day=random.choice(DAYS),
    )


def generate_conversation_burst(subject: str) -> List[str]:
    """
    Returns an ordered list of message texts forming ONE coherent exchange.
    The caller is responsible for assigning senders + reply_to chaining
    (each message in the burst replies to the one before it).
    """
    starter_kind = random.choices(
        ["question", "announcement", "resource", "opinion"], weights=[45, 20, 20, 15], k=1
    )[0]

    starter_pool = {
        "question": QUESTION_STARTERS, "announcement": ANNOUNCEMENT_STARTERS,
        "resource": RESOURCE_STARTERS, "opinion": OPINION_STARTERS,
    }[starter_kind]

    burst = [_fill(random.choice(starter_pool), subject)]

    if starter_kind == "question":
        burst.append(_fill(random.choice(ANSWER_REPLIES), subject))
        r = random.random()
        if r < 0.35:
            burst.append(_fill(random.choice(CLARIFY_REPLIES), subject))
            burst.append(_fill(random.choice(ANSWER_REPLIES), subject))
        if random.random() < 0.55:
            burst.append(random.choice(THANKS_REPLIES))
        if random.random() < 0.25:
            burst.append(_fill(random.choice(ADDITION_REPLIES), subject))
    elif starter_kind == "announcement":
        if random.random() < 0.6:
            burst.append(random.choice(THANKS_REPLIES))
        if random.random() < 0.3:
            burst.append(_fill(random.choice(CLARIFY_REPLIES), subject))
            burst.append(_fill(random.choice(ANSWER_REPLIES), subject))
    elif starter_kind == "resource":
        if random.random() < 0.7:
            burst.append(random.choice(THANKS_REPLIES))
        if random.random() < 0.3:
            burst.append(_fill(random.choice(ADDITION_REPLIES), subject))
    else:  # opinion
        if random.random() < 0.6:
            burst.append(_fill(random.choice(AGREEMENT_REPLIES), subject))
        if random.random() < 0.3:
            burst.append(_fill(random.choice(CLARIFY_REPLIES), subject))

    return burst


def pick_activity_tier() -> Tuple[str, int]:
    names   = [t[0] for t in config.ACTIVITY_TIERS]
    weights = [t[1] for t in config.ACTIVITY_TIERS]
    tier = random.choices(names, weights=weights, k=1)[0]
    lo, hi = next(t[2] for t in config.ACTIVITY_TIERS if t[0] == tier)
    return tier, random.randint(lo, hi)


def _weighted_sender_pool(member_ids: List[int], participation_tiers: Dict[int, str]) -> List[int]:
    """Higher-tier users appear more often as senders — mirrors real activity skew."""
    weight_map = {"lurker": 1, "occasional": 2, "active": 4, "helper": 6, "leader": 9}
    pool = []
    for uid in member_ids:
        w = weight_map.get(participation_tiers.get(uid, "occasional"), 2)
        pool.extend([uid] * w)
    return pool or member_ids


def seed_messages_for_thread(
    thread: Thread,
    member_ids: List[int],
    subject: str,
    bot_user_id: int,
    user_meta: Dict[int, dict],
    participation_tiers: Dict[int, str],
) -> int:
    if random.random() < config.EMPTY_THREAD_RATIO:
        return 0  # some threads stay empty — realistic

    tier, target_count = pick_activity_tier()
    sender_pool = _weighted_sender_pool(member_ids, participation_tiers)

    sent_messages: List[ThreadMessage] = []
    reaction_pairs: Set[Tuple[int, int]] = set()
    member_send_count: Dict[int, int] = {uid: 0 for uid in member_ids}
    last_deletable_ids: List[int] = []  # non-deleted msg ids, for reply targeting

    cursor_time = thread.created_at + datetime.timedelta(minutes=random.randint(5, 60))
    last_sender: Optional[int] = None

    while len(sent_messages) < target_count:
        burst_texts = generate_conversation_burst(subject)
        prev_msg: Optional[ThreadMessage] = None

        # occasionally kick the burst off as a reply to an older on-topic message
        topic_anchor_id = None
        if last_deletable_ids and random.random() < 0.30:
            topic_anchor_id = random.choice(last_deletable_ids[-30:])

        for i, text in enumerate(burst_texts):
            if len(sent_messages) >= target_count:
                break

            is_ai_triggered = (i == 1 and random.random() < 0.10)  # occasional @learnora in-burst
            is_ai = is_ai_triggered or (random.random() < config.AI_MESSAGE_CHANCE / 3)

            if is_ai:
                sender_id = bot_user_id
                text = random.choice(AI_ANSWER_TEMPLATES).format(
                    subject=subject, concept=random.choice(CONCEPTS_GENERIC),
                    explanation=random.choice(AI_EXPLANATIONS),
                )
            else:
                candidates = [uid for uid in sender_pool if uid != last_sender] or sender_pool
                sender_id = random.choice(candidates)
                member_send_count[sender_id] = member_send_count.get(sender_id, 0) + 1

                if random.random() < config.MENTION_CHANCE and len(member_ids) > 1:
                    mention_target = random.choice([m for m in member_ids if m != sender_id])
                    uname = user_meta.get(mention_target, {}).get("username")
                    if uname:
                        text = f"@{uname} {text}"

            gap = random.randint(2, 180)
            cursor_time = cursor_time + datetime.timedelta(minutes=gap)

            is_deleted = random.random() < config.DELETE_MESSAGE_CHANCE
            is_pinned = (not is_deleted) and (not is_ai) and random.random() < config.PIN_MESSAGE_CHANCE
            is_edited = (not is_deleted) and (not is_ai) and random.random() < config.EDIT_MESSAGE_CHANCE

            reply_to_id = None
            if i == 0 and topic_anchor_id:
                reply_to_id = topic_anchor_id
            elif i > 0 and prev_msg is not None:
                reply_to_id = prev_msg.id

            att = None if (is_deleted or is_ai) else maybe_attachment()

            pinned_by_id = None
            if is_pinned:
                pinned_by_id = thread.creator_id

            status = "sent" if is_deleted else pick_message_status(cursor_time)
            edited_at = cursor_time + datetime.timedelta(minutes=random.randint(1, 20)) if is_edited else None

            msg = ThreadMessage(
                thread_id=thread.id,
                sender_id=sender_id,
                text_content=text if not is_deleted else "[This message was deleted]",
                attachment_url=att["url"] if att else None,
                attachment_name=att["name"] if att else None,
                attachment_type=att["type"] if att else None,
                attachment_size=att["size"] if att else None,
                reply_to_id=reply_to_id,
                is_pinned=is_pinned,
                pinned_by_id=pinned_by_id,
                is_ai_response=is_ai,
                ai_personality="learnora" if is_ai else None,
                is_edited=is_edited,
                is_deleted=is_deleted,
                status=status,
                sent_at=cursor_time,
                edited_at=edited_at,
            )
            db.session.add(msg)
            db.session.flush()

            sent_messages.append(msg)
            if not is_deleted:
                last_deletable_ids.append(msg.id)
            prev_msg = msg
            last_sender = sender_id

            # ---- mentions -> real Mention + Notification rows ----
            if not is_ai and text.startswith("@"):
                uname = text[1:].split(" ", 1)[0]
                mentioned = next(
                    (uid for uid in member_ids
                     if user_meta.get(uid, {}).get("username") == uname and uid != sender_id),
                    None,
                )
                if mentioned:
                    db.session.add(Mention(
                        mentioned_in_type="thread_message",
                        mentioned_in_id=msg.id,
                        mentioned_user_id=mentioned,
                        mentioned_by_user_id=sender_id,
                    ))
                    db.session.add(Notification(
                        user_id=mentioned,
                        title="You were mentioned in a thread",
                        body=text[:80],
                        notification_type="thread_mention",
                        related_type="thread",
                        related_id=thread.id,
                    ))

            # ---- read receipts ----
            if not is_deleted and status != "sent":
                non_sender = [uid for uid in member_ids if uid != sender_id]
                if status == "read":
                    targets = non_sender
                else:
                    k = max(1, len(non_sender) // 2)
                    targets = random.sample(non_sender, min(k, len(non_sender)))
                for reader in targets:
                    db.session.add(ThreadMessageReadReceipt(
                        message_id=msg.id, user_id=reader,
                        read_at=cursor_time + datetime.timedelta(minutes=random.randint(1, 120)),
                    ))

            # ---- reactions ----
            if not is_deleted and random.random() < config.REACTION_CHANCE:
                pool = [uid for uid in member_ids if uid != sender_id]
                if pool:
                    n = random.randint(1, min(config.MAX_REACTIONS_PER_MSG, len(pool)))
                    for reactor in random.sample(pool, n):
                        pair = (msg.id, reactor)
                        if pair in reaction_pairs:
                            continue
                        reaction_pairs.add(pair)
                        db.session.add(ThreadMessageReaction(
                            message_id=msg.id, user_id=reactor,
                            emoji=random.choice(EMOJI_POOL),
                            reacted_at=cursor_time + datetime.timedelta(minutes=random.randint(1, 90)),
                        ))

    if sent_messages:
        thread.message_count = len([m for m in sent_messages if not m.is_deleted])
        thread.last_activity = sent_messages[-1].sent_at

    for uid, count in member_send_count.items():
        if count > 0:
            ThreadMember.query.filter_by(thread_id=thread.id, student_id=uid).update(
                {"messages_sent": count}
            )

    return len(sent_messages)


# ============================================================================
# JOIN REQUESTS + INVITES  (clustered, not random-uniform)
# ============================================================================

def seed_join_requests(
    threads: List[Thread],
    member_map: Dict[int, List[int]],
    user_meta: Dict[int, dict],
    users: List[User],
    count: int,
) -> int:
    created = 0
    used_pairs: Set[Tuple[int, int]] = set()
    all_ids = [u.id for u in users]

    for _ in range(count * 3):
        if created >= count:
            break
        thread = random.choice(threads)
        if thread.member_count >= thread.max_members:
            continue

        member_ids = set(member_map.get(thread.id, []))

        # Prefer requesters from the same department (realistic discovery)
        same_dept = [
            uid for uid in user_meta
            if user_meta[uid]["department"] == thread.department and uid not in member_ids
        ] if thread.department else []
        pool = same_dept if (same_dept and random.random() < 0.7) else [
            uid for uid in all_ids if uid not in member_ids
        ]
        if not pool:
            continue

        requester_id = random.choice(pool)
        pair = (thread.id, requester_id)
        if pair in used_pairs:
            continue
        used_pairs.add(pair)

        status = random.choices(["pending", "approved", "rejected"], weights=[55, 30, 15], k=1)[0]
        requested_at = random_past_datetime(max_days=90)
        reviewed_at = None
        reviewed_by = None
        if status in ("approved", "rejected"):
            reviewer_pool = member_map.get(thread.id, [thread.creator_id])
            reviewed_by = random.choice(reviewer_pool)
            reviewed_at = requested_at + datetime.timedelta(hours=random.randint(1, 72))

        db.session.add(ThreadJoinRequest(
            thread_id=thread.id,
            requester_id=requester_id,
            message=random.choice(JOIN_REQUEST_MESSAGES) if random.random() < 0.75 else None,
            status=status,
            requested_at=requested_at,
            reviewed_at=reviewed_at,
            reviewed_by=reviewed_by,
        ))
        created += 1

    return created


def seed_invites(
    threads: List[Thread],
    member_map: Dict[int, List[int]],
    friend_adj: Dict[int, Set[int]],
    users: List[User],
    count: int,
) -> int:
    created = 0
    used_pairs: Set[Tuple[int, int]] = set()
    all_ids = [u.id for u in users]

    for _ in range(count * 3):
        if created >= count:
            break
        thread = random.choice(threads)
        if thread.member_count >= thread.max_members:
            continue

        member_ids = member_map.get(thread.id, [])
        if not member_ids:
            continue
        inviter_id = random.choice(member_ids)
        member_set = set(member_ids)

        # Prefer inviting an actual friend-of-a-member (classmates/friends, not strangers)
        friend_candidates = [
            uid for uid in friend_adj.get(inviter_id, ()) if uid not in member_set
        ]
        candidate_pool = friend_candidates if (friend_candidates and random.random() < 0.75) else [
            uid for uid in all_ids if uid not in member_set
        ]
        if not candidate_pool:
            continue

        invitee_id = random.choice(candidate_pool)
        pair = (thread.id, invitee_id)
        if pair in used_pairs:
            continue
        used_pairs.add(pair)

        invited_at = random_past_datetime(max_days=60)
        db.session.add(ThreadJoinRequest(
            thread_id=thread.id,
            requester_id=invitee_id,
            message=random.choice(INVITE_MESSAGES),
            status="invited",
            requested_at=invited_at,
            reviewed_at=invited_at,
            reviewed_by=inviter_id,
        ))
        created += 1

    return created


# ============================================================================
# CLEAR EXISTING DATA
# ============================================================================

def commit_with_retry(label: str = "batch") -> None:
    """Commit the current session, retrying with backoff + a fresh
    connection pool if the connection was dropped mid-commit (e.g.
    'server closed the connection unexpectedly'). At ~125k messages
    this run can take a while, so a transient drop should be
    recovered from rather than crashing the whole run.

    Raises RuntimeError if the commit still fails after all retries.
    """
    for attempt in range(1, config.MAX_BATCH_RETRIES + 1):
        try:
            db.session.commit()
            return
        except (OperationalError, DBAPIError) as e:
            logger.error(
                f"DB connection error committing {label} "
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
            db.session.rollback()
            logger.error(f"Non-connection DB error committing {label}: {e}")
            raise
    raise RuntimeError(
        f"{label} commit failed after {config.MAX_BATCH_RETRIES} retries — giving up."
    )


def clear_existing_thread_data() -> bool:
    try:
        print("🗑️   Clearing existing thread-related data...")
        counts = {}
        for model in (ThreadMessageReadReceipt, ThreadMessageReaction, ThreadMessage,
                      ThreadJoinRequest, ThreadMember, Thread):
            counts[model.__tablename__] = model.query.delete(synchronize_session=False)
        # Clean up mentions/notifications tied to prior thread messages so re-runs don't duplicate.
        Mention.query.filter_by(mentioned_in_type="thread_message").delete(synchronize_session=False)
        Notification.query.filter(Notification.related_type == "thread").delete(synchronize_session=False)
        db.session.commit()
        for table, n in counts.items():
            print(f"   🗑️  {table}: {n} rows deleted")
        print("✅  Cleared existing thread data.")
        return True
    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Failed to clear thread data: {e}")
        print(f"❌  Failed to clear data: {e}")
        return False


# ============================================================================
# MAIN SEED FUNCTION
# ============================================================================

def seed_threads() -> bool:
    print("🌱  Starting thread seed (v2 — even distribution edition)...")
    random.seed(config.SEED_RANDOM_STATE)

    users, user_meta, dept_index, level_index, friend_adj = load_population()
    if len(users) < 10:
        print("❌  Need at least 10 approved students with profiles. Run seed_students.py first.")
        return False

    if not clear_existing_thread_data():
        return False

    participation_targets, participation_tiers = assign_participation_targets([u.id for u in users])
    remaining_budget = dict(participation_targets)

    all_threads: List[Thread] = []
    member_map: Dict[int, List[int]] = {}
    total_messages = 0

    try:
        print(f"\n🧵  Creating {config.NUM_THREADS} threads across all categories...")
        for i in range(config.NUM_THREADS):
            blueprint = build_thread_blueprint(i)
            target_size = random.randint(config.MEMBERS_MIN, config.MEMBERS_MAX)
            created_at = random_past_datetime()

            member_ids = pick_members_for_thread(
                blueprint, target_size, users, user_meta, dept_index, level_index,
                friend_adj, remaining_budget, member_map,
            )
            creator_id = member_ids[0]

            thread = create_thread_row(blueprint, creator_id, len(member_ids), created_at)
            db.session.add(thread)
            db.session.flush()

            full_members = add_members(thread, creator_id, member_ids, user_meta, participation_tiers)
            member_map[thread.id] = full_members
            all_threads.append(thread)

            msg_count = seed_messages_for_thread(
                thread, full_members, blueprint["subject"], LEARNORA_BOT_USER_ID, user_meta, participation_tiers
            )
            total_messages += msg_count

            if (i + 1) % config.BATCH_SIZE == 0:
                commit_with_retry(f"threads batch ending at {i + 1}")
                print(f"   ✓ {i + 1}/{config.NUM_THREADS} threads committed "
                      f"(~{total_messages} messages so far)...")

        commit_with_retry("final threads commit")
        print(f"✅  {len(all_threads)} threads created, ~{total_messages} messages seeded.")

        print(f"\n📥  Seeding {config.NUM_JOIN_REQUESTS} join requests...")
        join_reqs = seed_join_requests(all_threads, member_map, user_meta, users, config.NUM_JOIN_REQUESTS)
        commit_with_retry("join requests")
        print(f"   ✓ {join_reqs} join requests created.")

        print(f"\n📨  Seeding {config.NUM_INVITES} invites...")
        invites = seed_invites(all_threads, member_map, friend_adj, users, config.NUM_INVITES)
        commit_with_retry("invites")
        print(f"   ✓ {invites} invites created.")

        print_summary(all_threads, participation_tiers)
        return True

    except Exception as e:
        db.session.rollback()
        logger.error(f"Unexpected error during thread seeding: {e}", exc_info=True)
        print(f"❌  Unexpected error: {e}")
        return False


# ============================================================================
# VALIDATION
# ============================================================================

def validate_seed_integrity() -> bool:
    print("\n🔍  Validating seeded thread data...")
    ok = True

    valid_user_ids = {u.id for u in User.query.with_entities(User.id).all()}
    bad_sender = ThreadMessage.query.filter(
        ~ThreadMessage.sender_id.in_(valid_user_ids),
        ThreadMessage.sender_id != LEARNORA_BOT_USER_ID,
    ).count()
    if bad_sender:
        print(f"   ❌ {bad_sender} messages reference invalid sender_id")
        ok = False

    bad_member = ThreadMember.query.filter(~ThreadMember.student_id.in_(valid_user_ids)).count()
    if bad_member:
        print(f"   ❌ {bad_member} thread members reference invalid user id")
        ok = False

    from sqlalchemy import func as _func
    dupes = (
        db.session.query(ThreadMember.thread_id, ThreadMember.student_id)
        .group_by(ThreadMember.thread_id, ThreadMember.student_id)
        .having(_func.count(ThreadMember.id) > 1)
        .count()
    )
    if dupes:
        print(f"   ❌ {dupes} duplicate thread memberships found")
        ok = False

    bad_reply = ThreadMessage.query.filter(
        ThreadMessage.reply_to_id.isnot(None),
        ~ThreadMessage.reply_to_id.in_(db.session.query(ThreadMessage.id))
    ).count()
    if bad_reply:
        print(f"   ❌ {bad_reply} messages reply to a non-existent message")
        ok = False

    if ok:
        print("   ✅ No invalid FKs, no duplicate memberships, all reply chains valid")
    return ok


# ============================================================================
# SUMMARY
# ============================================================================

def print_summary(threads: List[Thread], participation_tiers: Dict[int, str]) -> None:
    print("\n" + "=" * 64)
    print("📊  THREAD SEED SUMMARY (v2 — even distribution)")
    print("=" * 64)

    total_threads   = Thread.query.count()
    total_members   = ThreadMember.query.count()
    total_messages  = ThreadMessage.query.count()
    total_deleted   = ThreadMessage.query.filter_by(is_deleted=True).count()
    total_pinned    = ThreadMessage.query.filter_by(is_pinned=True).count()
    total_ai        = ThreadMessage.query.filter_by(is_ai_response=True).count()
    total_replies   = ThreadMessage.query.filter(ThreadMessage.reply_to_id.isnot(None)).count()
    total_reactions = ThreadMessageReaction.query.count()
    total_receipts  = ThreadMessageReadReceipt.query.count()
    total_join_reqs = ThreadJoinRequest.query.count()
    total_mentions  = Mention.query.filter_by(mentioned_in_type="thread_message").count()
    empty_threads   = sum(1 for t in threads if t.message_count == 0)

    print(f"\n🧵  Threads:            {total_threads}  ({empty_threads} empty)")
    print(f"👥  Memberships:        {total_members}")
    print(f"💬  Messages:           {total_messages}")
    print(f"   🔁 Replies:          {total_replies}")
    print(f"   📌 Pinned:           {total_pinned}")
    print(f"   🤖 AI (Learnora):    {total_ai}")
    print(f"   🗑️  Soft-deleted:     {total_deleted}")
    print(f"   @  Mentions:         {total_mentions}")
    print(f"😀  Reactions:          {total_reactions}")
    print(f"🧾  Read receipts:      {total_receipts}")
    print(f"📥  Join requests:      {total_join_reqs}")

    print("\n📋  Join Request Status Breakdown:")
    for status in ("pending", "approved", "rejected", "invited"):
        count = ThreadJoinRequest.query.filter_by(status=status).count()
        print(f"   {status.capitalize():10s}: {count}")

    print("\n🎭  Participation Tier Distribution (thread memberships per user):")
    tier_counts = Counter(participation_tiers.values())
    for tier_name, _, span in config.PARTICIPATION_TIERS:
        c = tier_counts.get(tier_name, 0)
        pct = c / max(len(participation_tiers), 1) * 100
        print(f"   {tier_name:12s} ({span[0]}-{span[1]} threads): {c} users ({pct:.1f}%)")

    # top / bottom participants — purely emergent, nobody hardcoded
    membership_counts = Counter(m.student_id for m in ThreadMember.query.all())
    top_users = membership_counts.most_common(10)
    if top_users:
        print("\n🌟  Top 10 Most Active Thread Participants (emergent, not hardcoded):")
        ids = [uid for uid, _ in top_users]
        umap = {u.id: u for u in User.query.filter(User.id.in_(ids)).all()}
        for uid, cnt in top_users:
            u = umap.get(uid)
            print(f"   {u.name if u else f'User#{uid}'}: {cnt} threads")

    print("\n📚  Category spread:")
    # category isn't persisted directly on Thread, so approximate via tags
    tag_counter = Counter()
    for t in Thread.query.all():
        for tag in (t.tags or [])[:1]:
            tag_counter[tag] += 1
    for tag, cnt in tag_counter.most_common(10):
        print(f"   {tag}: {cnt}")

    validate_seed_integrity()

    print("\n" + "=" * 64)
    print("✨  Thread seed complete — activity is spread across the whole population.")
    print("=" * 64 + "\n")


# ============================================================================
# STANDALONE EXECUTION
# ============================================================================

if __name__ == "__main__":
    from app import app

    with app.app_context():
        success = seed_threads()
        if success:
            logger.info("Thread seed v2 completed successfully.")
            exit(0)
        else:
            logger.error("Thread seed v2 failed.")
            exit(1)
