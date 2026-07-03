"""
StudyHub - Homework Feed: Population-Wide Seed Script
========================================================
Generates realistic homework/assignment + submission data across the ENTIRE
seeded student population (not just a single primary user), so the homework
feed, browse-connections-homework, streaks, champions, and stats endpoints
all have rich, believable data to work against.

Why this differs from homework_seed.py
---------------------------------------
homework_seed.py builds a deep, hand-curated dataset around a single primary
QA account (User ID=1). This script instead spreads assignments and helper
submissions across every approved student, using the same sociability-tier
idea from connection_seed-1.py (a few power users, many light users, a long
tail of dormant ones) so the feed looks like it came from thousands of real
students rather than one test account. Reuses the connection graph produced
by connection_seed-1.py to decide who is *eligible* to help whom, exactly
the way offer_homework_help() in homework_system.py enforces it (helper and
requester must have an accepted Connection).

Run AFTER seed_students.py and connection_seed-1.py.

Usage:
    python homework_feed_seed.py                 # seed on top of existing data
    python homework_feed_seed.py --clear          # wipe Assignment/HomeworkSubmission first
    python homework_feed_seed.py --dry-run        # preview counts, write nothing
    python homework_feed_seed.py --no-extras       # skip notifications/activity feed
    python homework_feed_seed.py --seed 7          # override RNG seed

Approximate output at the default config, with ~3,000 approved students
(matches seed_students.py's default population):
    ~15,500 Assignment rows      (≈45% marked is_shared_for_help)
    ~ 9,500 HomeworkSubmission rows (only on shared assignments, helpers
                                      drawn strictly from accepted connections)
    ---------------------------------------------------------------
    ~25,000 combined homework-feed records
    + a bounded number of supporting Notification / ActivityFeed rows
      (not counted toward the 25k target — see Phase 7/8)

Every count above scales with the actual population size found in the DB,
so if you seeded a different number of students the totals will move
proportionally; the script prints the real numbers it produced at the end.
"""

import sys
import random
import logging
import argparse
import datetime
from typing import List, Dict, Set, Tuple, Optional
from collections import Counter, defaultdict

from sqlalchemy.exc import SQLAlchemyError, IntegrityError

try:
    from app import app
    from extensions import db
    from models import (
        User, StudentProfile, Connection, Assignment, HomeworkSubmission,
        Notification, ActivityFeed
    )
except ImportError as exc:
    print(f"❌ Import error: {exc}")
    print("   Make sure you run this from your project root directory.")
    sys.exit(1)

# ============================================================================
# CONFIGURATION
# ============================================================================

class SeedConfig:
    RANDOM_SEED = 2024
    BATCH_SIZE = 500

    # ── Sociability tiers for HOMEWORK usage (not the same as the social
    #    graph tiers in connection_seed-1.py — someone can have 200 friends
    #    and still barely use the homework feature, and vice versa).
    #    (name, selection_weight, (min_assignments, max_assignments)) ────────
    USAGE_TIERS = [
        ("dormant",  30, (0, 1)),
        ("light",    35, (2, 4)),
        ("regular",  25, (5, 9)),
        ("active",    8, (10, 18)),
        ("power",     2, (20, 35)),
    ]

    # ── Sharing behaviour ────────────────────────────────────────────────
    SHARE_PROB_WITH_CONNECTIONS = 0.45
    SHARE_PROB_NO_CONNECTIONS   = 0.05   # shares anyway, just gets 0 helpers

    # ── Helper count per shared assignment (only drawn if owner has ties) ──
    HELPER_COUNT_CHOICES = [0, 1, 2, 3, 4]
    HELPER_COUNT_WEIGHTS = [0.15, 0.45, 0.25, 0.10, 0.05]

    # ── Submission lifecycle mix ────────────────────────────────────────
    LIFECYCLE_STAGES  = ["pending", "submitted", "reviewed", "completed"]
    LIFECYCLE_WEIGHTS = [0.15, 0.20, 0.20, 0.45]

    # ── Date windows ─────────────────────────────────────────────────────
    PAST_DAYS   = 120
    FUTURE_DAYS = 30

    # ── Supporting data (bounded, not part of the 25k homework target) ────
    NOTIFICATION_SAMPLE_RATE = 0.12   # fraction of submissions that get a notif
    ACTIVITY_RECENT_HOURS    = 20     # ActivityFeed rows must be "recent"
    ACTIVITY_TARGET_ROWS     = 600

config = SeedConfig()

# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("seed_homework_feed.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================================================
# DATA POOLS
# ============================================================================

SUBJECTS = [
    "Mathematics", "Physics", "Chemistry", "Biology",
    "Computer Science", "Data Structures", "Algorithms",
    "Linear Algebra", "Calculus", "Statistics",
    "Database Systems", "Web Development", "Machine Learning",
    "Discrete Math", "Operating Systems", "Software Engineering",
    "English Literature", "History", "Economics", "Psychology",
    "Philosophy", "Sociology", "Political Science", "Geography",
]

DIFFICULTIES = ["easy", "medium", "hard"]
DIFFICULTY_WEIGHTS = [0.25, 0.45, 0.30]

# Loosely ties a student's real department (from StudentProfile) to the
# subjects they're most likely to be assigned homework in. Falls back to a
# fully random subject when no keyword matches — keeps things realistic
# without needing a hand-curated entry for all 80+ departments.
DEPARTMENT_SUBJECT_AFFINITY = {
    "Computer": ["Computer Science", "Data Structures", "Algorithms",
                 "Database Systems", "Web Development", "Machine Learning",
                 "Operating Systems", "Software Engineering", "Discrete Math"],
    "Software": ["Software Engineering", "Web Development", "Database Systems",
                 "Algorithms", "Data Structures"],
    "Data Science": ["Machine Learning", "Statistics", "Data Structures", "Algorithms"],
    "Cyber Security": ["Computer Science", "Operating Systems", "Database Systems"],
    "Mathemat": ["Calculus", "Linear Algebra", "Statistics", "Discrete Math"],
    "Statistics": ["Statistics", "Calculus"],
    "Physi": ["Physics", "Calculus"],
    "Chemi": ["Chemistry"],
    "Biolog": ["Biology"],
    "Microbiolog": ["Biology", "Chemistry"],
    "Biochemistry": ["Biology", "Chemistry"],
    "Economics": ["Economics", "Statistics"],
    "Accounting": ["Economics", "Statistics"],
    "Banking": ["Economics", "Statistics"],
    "Business": ["Economics"],
    "Psychology": ["Psychology"],
    "Sociology": ["Sociology"],
    "Political Science": ["Political Science", "History"],
    "International Relations": ["Political Science", "History"],
    "Philosophy": ["Philosophy"],
    "Geograph": ["Geography"],
    "History": ["History"],
    "English": ["English Literature"],
    "Mass Communication": ["English Literature", "Sociology"],
    "Electrical": ["Physics", "Computer Science"],
    "Civil Engineering": ["Physics", "Calculus"],
    "Mechanical Engineering": ["Physics", "Calculus"],
    "Chemical Engineering": ["Chemistry", "Calculus"],
}

ASSIGNMENT_TITLES = {
    "Mathematics": [
        "Integration by Parts Problem Set", "Differential Equations Worksheet",
        "Series and Sequences Assignment", "Matrix Operations Exercise",
        "Probability Theory Task",
    ],
    "Physics": [
        "Kinematics Lab Report", "Electromagnetic Fields Problem Set",
        "Quantum Mechanics Homework", "Thermodynamics Assignment", "Wave Optics Exercise",
    ],
    "Chemistry": [
        "Organic Chemistry Reaction Mechanisms", "Stoichiometry Problem Set",
        "Electrochemistry Lab Report", "Acid-Base Equilibrium Assignment",
        "Molecular Orbital Theory Exercise",
    ],
    "Biology": [
        "Cell Division and Mitosis Report", "Genetics Punnett Square Assignment",
        "Ecosystem Analysis Essay", "Protein Synthesis Problem Set",
        "Evolution and Natural Selection Review",
    ],
    "Computer Science": [
        "Binary Search Tree Implementation", "Sorting Algorithm Analysis",
        "Recursion Problem Set", "OOP Design Exercise", "Complexity Theory Assignment",
    ],
    "Data Structures": [
        "Linked List Implementation", "Stack and Queue Problems",
        "Graph Traversal Assignment", "Hash Table Design Exercise",
        "Heap and Priority Queue Task",
    ],
    "Algorithms": [
        "Dynamic Programming Problems", "Greedy Algorithm Assignment",
        "Divide and Conquer Exercise", "Graph Shortest Path Problems",
        "NP-Completeness Analysis",
    ],
    "Linear Algebra": [
        "Eigenvalue and Eigenvector Problems", "Vector Spaces Assignment",
        "Linear Transformation Exercise", "Matrix Decomposition Task",
        "Orthogonality Problem Set",
    ],
    "Calculus": [
        "Multivariable Calculus Problem Set", "Taylor Series Expansion Exercise",
        "Double and Triple Integrals", "Gradient and Directional Derivatives",
        "Optimization Problems",
    ],
    "Statistics": [
        "Hypothesis Testing Assignment", "Regression Analysis Task",
        "Probability Distributions Exercise", "Confidence Intervals Problem Set",
        "ANOVA and Chi-Square Test",
    ],
    "Database Systems": [
        "SQL Query Optimization Task", "ER Diagram Design Assignment",
        "Normalization Exercise", "Transaction Management Problem Set",
        "NoSQL Database Comparison Report",
    ],
    "Web Development": [
        "React Component Architecture", "REST API Design Assignment",
        "CSS Flexbox and Grid Exercise", "Authentication Flow Implementation",
        "Database Integration Task",
    ],
    "Machine Learning": [
        "Linear Regression Implementation", "Neural Network Design Exercise",
        "Feature Engineering Assignment", "Model Evaluation Problem Set",
        "Clustering Algorithm Task",
    ],
    "Discrete Math": [
        "Graph Theory Problem Set", "Combinatorics Assignment",
        "Boolean Algebra Exercise", "Set Theory and Logic Task", "Number Theory Problems",
    ],
    "Operating Systems": [
        "Process Scheduling Assignment", "Memory Management Exercise",
        "File System Design Task", "Deadlock Detection Problem Set",
        "Concurrency and Synchronization",
    ],
    "Software Engineering": [
        "UML Diagram Design Exercise", "Agile Sprint Planning Assignment",
        "Code Review and Refactoring Task", "Testing Strategy Problem Set",
        "Design Patterns Implementation",
    ],
    "English Literature": [
        "Close Reading Response Essay", "Comparative Poetry Analysis",
        "Character Study Assignment", "Thematic Essay Draft",
    ],
    "History": [
        "Primary Source Analysis", "Historiography Essay",
        "Timeline and Causation Assignment", "Comparative Case Study",
    ],
    "Economics": [
        "Supply and Demand Case Study", "Macroeconomic Indicators Report",
        "Market Structure Analysis", "Policy Impact Essay",
    ],
    "Psychology": [
        "Cognitive Bias Case Study", "Research Methods Critique",
        "Behavioural Experiment Write-Up", "Developmental Stages Essay",
    ],
    "Philosophy": [
        "Argument Reconstruction Exercise", "Ethical Dilemma Essay",
        "Critical Response to a Primary Text", "Logic and Fallacies Worksheet",
    ],
    "Sociology": [
        "Social Structures Case Study", "Survey Design Assignment",
        "Community Fieldwork Report", "Theory Application Essay",
    ],
    "Political Science": [
        "Comparative Government Essay", "Policy Brief Assignment",
        "Electoral Systems Case Study", "International Relations Analysis",
    ],
    "Geography": [
        "Urbanisation Case Study", "Climate Data Analysis",
        "Fieldwork Report", "GIS Mapping Exercise",
    ],
}
GENERIC_TITLES = [
    "Chapter Review and Analysis", "Weekly Problem Set", "Lab Report Submission",
    "Research Essay Draft", "End-of-Unit Assignment", "Concept Application Exercise",
    "Critical Thinking Task", "Group Project Contribution", "Seminar Presentation Prep",
    "Final-Year Project Milestone", "Reading Response Journal",
]

DESCRIPTIONS = [
    "Complete all questions thoroughly. Show your working where applicable.",
    "Refer to the textbook chapters 4–6 for background reading before starting.",
    "This is worth 20% of the final grade – take your time and be precise.",
    "You may discuss approaches with classmates but final answers must be your own.",
    "Submit via the online portal. Late submissions incur a 10% daily penalty.",
    "Use diagrams where they help clarify your explanations.",
    "Include at least three cited references in your write-up.",
    "Code tasks must include unit tests for full marks.",
    "Focus on efficiency – brute-force solutions will receive partial credit only.",
    "Compare at least two approaches and justify your chosen method.",
    "Ensure your report follows the standard format outlined in the course guide.",
    "Pair up with a study partner if you find parts challenging.",
    "This is a group submission — coordinate with your assigned team.",
    "Bring printed copies to the seminar for peer review.",
    "Cross-check your results against the sample dataset provided in class.",
]

SOLUTION_TEXTS = [
    "Here's my step-by-step breakdown:\n\n1. First I identified the key variables and constraints.\n2. Applied the relevant theorem to simplify the expression.\n3. Worked through the algebra carefully, checking each line.\n4. The final answer comes out to the value shown below.\n\nLet me know if any step is unclear and I can elaborate!",
    "I solved this by breaking it into smaller sub-problems:\n\n**Part A:** Used integration by substitution. Let u = 3x + 1, then du = 3dx.\n**Part B:** Applied the chain rule. The derivative is as follows...\n**Part C:** Combined both results to get the final expression.\n\nHappy to walk through any part in more detail.",
    "Great question! The trick here is recognising the pattern early:\n\n- The recurrence relation simplifies to a closed form.\n- Once you see it as a geometric series, everything falls into place.\n- Substituting back in confirms the answer.\n\nCode implementation is attached in my resources.",
    "I approached this problem using dynamic programming:\n\n```\ndefine dp[i] = optimal solution up to index i\nbase case: dp[0] = 0\ntransition: dp[i] = max(dp[i-1], dp[i-2] + value[i])\n```\n\nTime complexity: O(n), Space: O(1) with optimisation.",
    "Here is my solution with full working:\n\n**Setup:** Drew out the system diagram first.\n**Analysis:** Identified all forces / variables acting on the system.\n**Calculation:** Applied Newton's second law / Kirchhoff's laws as appropriate.\n**Result:** The answer is consistent with the expected range from the textbook.\n\nDouble-check the sign conventions on your end!",
    "I researched this extensively and here's what I found:\n\nThe primary concept revolves around the principle of superposition. When applied to this particular scenario, the combined effect yields a net result that can be computed as follows...\n\nSources:\n- Textbook Chapter 7, pages 142–148\n- Additional reading from the course portal",
    "Solution using first principles:\n\n1. Start with the definition.\n2. Apply the relevant lemma proved in lecture 9.\n3. Simplify using the identity we derived last week.\n4. Final answer confirmed numerically.\n\nTook me a while but once I saw the substitution it clicked!",
    "My take on this: read through the source material twice before writing anything.\n\nMain argument: the author's central claim rests on three supporting points, which I've outlined with page references below.\n\nHappy to compare notes if your reading differed.",
]

FEEDBACK_TEXTS = [
    "This is exactly what I needed – thank you so much! The step-by-step breakdown made it really easy to follow along.",
    "Really helpful! I had the right idea but was making an error in step 3. Now I see where I went wrong.",
    "Perfect explanation. I especially appreciated the alternative approach you showed in Part B.",
    "Saved me hours! The code example was really clear and I managed to adapt it for my own solution.",
    "Good solution but I think there might be a small error in line 4 – the coefficient should be 2, not 3. Otherwise great!",
    "Thank you! I understood the concept but was struggling to apply it. Your worked example cleared everything up.",
    "Brilliant – I can see exactly where my logic was off now. Will definitely reach out again.",
    "This is very thorough. I'll study each step carefully before my exam tomorrow. Really appreciate it!",
    "Not quite what I was looking for, but it pointed me in the right direction. Thanks for taking the time.",
]

REACTION_TYPES = ["thanks", "lifesaver", "mind_blown", "perfect"]
REACTION_WEIGHTS = [0.30, 0.35, 0.15, 0.20]

# ============================================================================
# TIME / RANDOM HELPERS
# ============================================================================

def now() -> datetime.datetime:
    return datetime.datetime.utcnow()


def past(days: int = 0, hours: int = 0) -> datetime.datetime:
    return now() - datetime.timedelta(days=days, hours=hours)


def future(days: int = 0, hours: int = 0) -> datetime.datetime:
    return now() + datetime.timedelta(days=days, hours=hours)


def rand_past(min_days: int = 1, max_days: int = config.PAST_DAYS) -> datetime.datetime:
    return past(days=random.randint(min_days, max_days))


def rand_future_due() -> datetime.datetime:
    """Weighted due-date bucket: overdue / urgent / soon / upcoming."""
    roll = random.random()
    if roll < 0.15:
        return past(days=random.randint(1, 21))                          # overdue
    elif roll < 0.35:
        return future(hours=random.randint(1, 23))                        # due <24h
    elif roll < 0.55:
        return future(days=random.randint(2, 7))                          # due soon
    else:
        return future(days=random.randint(8, config.FUTURE_DAYS))         # upcoming


def pick_status_for_due(due_date: datetime.datetime) -> str:
    """Status distribution that's plausible given how close the due date is."""
    if due_date < now():
        return random.choices(
            ["completed", "not_started", "in_progress"], weights=[0.55, 0.30, 0.15]
        )[0]
    hours_left = (due_date - now()).total_seconds() / 3600
    if hours_left < 24:
        return random.choices(["not_started", "in_progress"], weights=[0.45, 0.55])[0]
    return random.choices(
        ["not_started", "in_progress", "completed"], weights=[0.45, 0.35, 0.20]
    )[0]


def pick_subject_for_department(department: Optional[str]) -> str:
    dept = department or ""
    for keyword, subjects in DEPARTMENT_SUBJECT_AFFINITY.items():
        if keyword.lower() in dept.lower() and random.random() < 0.75:
            return random.choice(subjects)
    return random.choice(SUBJECTS)


def pick_title(subject: str) -> str:
    titles = ASSIGNMENT_TITLES.get(subject, GENERIC_TITLES)
    return random.choice(titles)


def pick_difficulty() -> str:
    return random.choices(DIFFICULTIES, weights=DIFFICULTY_WEIGHTS)[0]


def pick_reaction() -> str:
    return random.choices(REACTION_TYPES, weights=REACTION_WEIGHTS)[0]


def rand_response_seconds(min_h: int = 1, max_h: int = 72) -> int:
    return random.randint(min_h * 3600, max_h * 3600)


def usage_tier_and_target() -> Tuple[str, int]:
    names = [t[0] for t in config.USAGE_TIERS]
    weights = [t[1] for t in config.USAGE_TIERS]
    chosen = random.choices(names, weights=weights, k=1)[0]
    lo, hi = next(t[2] for t in config.USAGE_TIERS if t[0] == chosen)
    return chosen, random.randint(lo, hi)

# ============================================================================
# DATABASE FETCH HELPERS
# ============================================================================

def fetch_population() -> List[Tuple[int, Optional[str]]]:
    """Single query: every approved user + their department. No N+1."""
    rows = (
        db.session.query(User.id, StudentProfile.department)
        .outerjoin(StudentProfile, StudentProfile.user_id == User.id)
        .filter(User.status == "approved")
        .all()
    )
    return [(uid, dept) for uid, dept in rows]


def build_connection_adjacency(user_ids: List[int]) -> Dict[int, List[int]]:
    """Single query for every accepted connection; build uid -> [connected uids]."""
    id_set = set(user_ids)
    conns = Connection.query.filter(Connection.status == "accepted").all()
    adjacency: Dict[int, List[int]] = defaultdict(list)
    for c in conns:
        if c.requester_id in id_set and c.receiver_id in id_set:
            adjacency[c.requester_id].append(c.receiver_id)
            adjacency[c.receiver_id].append(c.requester_id)
    return adjacency

# ============================================================================
# CLEAR EXISTING DATA
# ============================================================================

def clear_homework_data(force: bool = False) -> bool:
    try:
        sub_count = HomeworkSubmission.query.count()
        assign_count = Assignment.query.count()
        if (sub_count + assign_count) == 0:
            print("ℹ️  No existing homework data to clear.")
            return True
        if not force:
            print(f"\n⚠️  Found {assign_count} assignments and {sub_count} submissions.")
            resp = input("Clear all existing homework data? (yes/no): ")
            if resp.lower() != "yes":
                print("❌ Clear aborted.")
                return False
        print("🗑️  Clearing homework data …")
        HomeworkSubmission.query.delete()
        Assignment.query.delete()
        db.session.commit()
        print("✅ Homework data cleared.")
        return True
    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Clear failed: {e}")
        print(f"❌ Clear failed: {e}")
        return False

# ============================================================================
# FACTORIES
# ============================================================================

def make_assignment(owner_id: int, department: Optional[str], is_shared: bool) -> Assignment:
    subject = pick_subject_for_department(department)
    difficulty = pick_difficulty()
    due_date = rand_future_due()
    status = pick_status_for_due(due_date)
    created_at = rand_past(1, config.PAST_DAYS)

    est_hours = round(random.uniform(0.5, 8.0), 1) if random.random() < 0.75 else None
    time_spent = 0
    if status != "not_started" and est_hours:
        pct = random.uniform(0.1, 1.0) if status == "in_progress" else random.uniform(0.8, 1.2)
        time_spent = int(est_hours * 60 * pct)

    completed_at = None
    if status == "completed":
        completed_at = created_at + datetime.timedelta(
            hours=random.randint(1, int(max(est_hours or 2, 1) * 3))
        )

    a = Assignment(
        user_id=owner_id,
        title=pick_title(subject),
        subject=subject,
        description=random.choice(DESCRIPTIONS),
        difficulty=difficulty,
        status=status,
        estimated_hours=est_hours,
        time_spent_minutes=time_spent,
        is_shared_for_help=is_shared,
        due_date=due_date,
        created_at=created_at,
        completed_at=completed_at,
        resources=[],
        priority_score=0.0,
    )
    a.calculate_priority()
    return a


def make_submission(
    assignment_id: int, requester_id: int, helper_id: int,
    title: str, description: str, subject: str, difficulty: str,
    assignment_created_at: datetime.datetime, target_status: str,
) -> HomeworkSubmission:
    base_time = assignment_created_at + datetime.timedelta(hours=random.randint(1, 12))

    sub = HomeworkSubmission(
        assignment_id=assignment_id,
        requester_id=requester_id,
        helper_id=helper_id,
        title=title,
        description=description,
        subject=subject,
        difficulty=difficulty,
        status="pending",
        created_at=base_time,
        solution_resources=[],
        feedback_resources=[],
    )

    if target_status == "pending":
        return sub

    submitted_delta = datetime.timedelta(seconds=rand_response_seconds(1, 48))
    sub.submitted_at = base_time + submitted_delta
    sub.solution_text = random.choice(SOLUTION_TEXTS)
    sub.response_time_seconds = int(submitted_delta.total_seconds())
    sub.status = "submitted"
    if target_status == "submitted":
        return sub

    sub.feedback_at = sub.submitted_at + datetime.timedelta(hours=random.randint(1, 24))
    sub.feedback_text = random.choice(FEEDBACK_TEXTS)
    sub.feedback_rating = random.randint(3, 5)
    sub.status = "reviewed"
    if target_status == "reviewed":
        return sub

    sub.reaction_at = sub.feedback_at + datetime.timedelta(hours=random.randint(1, 12))
    sub.is_marked_helpful = True
    sub.reaction_type = pick_reaction()
    sub.status = "completed"
    return sub


def make_notification(user_id: int, notification_type: str, title: str, body: str,
                       related_id: Optional[int], created_at: datetime.datetime) -> Notification:
    is_read = random.random() < 0.55
    read_at = created_at + datetime.timedelta(hours=random.randint(1, 48)) if is_read else None
    return Notification(
        user_id=user_id, title=title, body=body, notification_type=notification_type,
        related_type="assignment", related_id=related_id, is_read=is_read,
        created_at=created_at, read_at=read_at,
        link=f"/homework/{related_id}" if related_id else None,
    )


def make_activity(user_id: int, activity_type: str, data: dict) -> ActivityFeed:
    created = now() - datetime.timedelta(hours=random.uniform(0, config.ACTIVITY_RECENT_HOURS))
    return ActivityFeed(
        user_id=user_id, activity_type=activity_type, activity_data=data,
        created_at=created, expires_at=created + datetime.timedelta(hours=24),
    )

# ============================================================================
# BATCH COMMIT HELPER
# ============================================================================

def batch_add_commit(items: List, label: str) -> Tuple[int, int]:
    ok, failed = 0, 0
    for i, item in enumerate(items, 1):
        db.session.add(item)
        ok += 1
        if i % config.BATCH_SIZE == 0:
            try:
                db.session.commit()
            except (SQLAlchemyError, IntegrityError) as e:
                db.session.rollback()
                logger.error(f"Batch commit error ({label} #{i}): {e}")
                failed += config.BATCH_SIZE
                ok -= config.BATCH_SIZE
    try:
        db.session.commit()
    except (SQLAlchemyError, IntegrityError) as e:
        db.session.rollback()
        logger.error(f"Final commit error ({label}): {e}")
        failed += ok
        ok = 0
    return ok, failed

# ============================================================================
# MAIN SEEDING LOGIC
# ============================================================================

def seed_homework_feed(dry_run: bool = False, with_extras: bool = True) -> bool:
    print("\n" + "=" * 68)
    print("📚 StudyHub Homework Feed – Population-Wide Seed")
    print("=" * 68)

    population = fetch_population()
    if len(population) < 2:
        print("❌ Need at least 2 approved students. Run seed_students.py first.")
        return False

    user_ids = [uid for uid, _ in population]
    dept_map = {uid: dept for uid, dept in population}
    print(f"✅ Found {len(population)} approved students")

    adjacency = build_connection_adjacency(user_ids)
    connected_users = sum(1 for uid in user_ids if adjacency.get(uid))
    print(f"🔗 {connected_users}/{len(population)} students have at least one "
          f"accepted connection (from connection_seed-1.py)")

    # ── Assign usage tiers up front so we can preview totals ───────────────
    tier_targets: Dict[int, Tuple[str, int]] = {uid: usage_tier_and_target() for uid in user_ids}
    projected_assignments = sum(t for _, t in tier_targets.values())
    tier_counts = Counter(t for t, _ in tier_targets.values())

    print(f"\n📈 Projected assignments (pre-generation): ~{projected_assignments}")
    for name, _, drange in config.USAGE_TIERS:
        c = tier_counts.get(name, 0)
        pct = c / max(len(user_ids), 1) * 100
        print(f"   {name:8s} ({drange[0]:>2}-{drange[1]:<2} assignments): "
              f"{c:5d} students ({pct:.1f}%)")

    if dry_run:
        est_shared = int(projected_assignments * 0.40)
        est_subs = int(est_shared * 1.4)
        print("\n🔍 DRY RUN – no data will be written.")
        print(f"   Would create ≈{projected_assignments} assignments")
        print(f"   Would create ≈{est_shared} of those as shared-for-help")
        print(f"   Would create ≈{est_subs} homework submissions")
        print(f"   Combined total ≈{projected_assignments + est_subs} homework-feed records")
        return True

    # ════════════════════════════════════════════════════════════════════
    # PHASE 1 – Assignments for every student
    # ════════════════════════════════════════════════════════════════════
    print(f"\n📝 Phase 1: Generating assignments for {len(user_ids)} students …")
    phase1_items = []
    for uid in user_ids:
        _, target = tier_targets[uid]
        has_conn = bool(adjacency.get(uid))
        share_prob = (config.SHARE_PROB_WITH_CONNECTIONS if has_conn
                      else config.SHARE_PROB_NO_CONNECTIONS)
        for _ in range(target):
            is_shared = random.random() < share_prob
            phase1_items.append(make_assignment(uid, dept_map.get(uid), is_shared))

    ok1, fail1 = batch_add_commit(phase1_items, "Phase1-Assignments")
    print(f"   ✅ {ok1} assignments created, {fail1} failed")

    # ════════════════════════════════════════════════════════════════════
    # PHASE 2 – Submissions on shared assignments (helpers = accepted
    #           connections of the assignment owner only)
    # ════════════════════════════════════════════════════════════════════
    print(f"\n🤝 Phase 2: Generating helper submissions on shared assignments …")

    shared_assignments = (
        Assignment.query
        .filter(Assignment.user_id.in_(user_ids), Assignment.is_shared_for_help.is_(True))
        .with_entities(
            Assignment.id, Assignment.user_id, Assignment.title, Assignment.description,
            Assignment.subject, Assignment.difficulty, Assignment.created_at
        )
        .all()
    )
    print(f"   Found {len(shared_assignments)} shared assignments to attach helpers to")

    phase2_items = []
    for a_id, owner_id, title, description, subject, difficulty, created_at in shared_assignments:
        candidates = adjacency.get(owner_id, [])
        if not candidates:
            continue
        num_helpers = random.choices(
            config.HELPER_COUNT_CHOICES, weights=config.HELPER_COUNT_WEIGHTS, k=1
        )[0]
        num_helpers = min(num_helpers, len(candidates))
        if num_helpers == 0:
            continue
        helpers = random.sample(candidates, num_helpers)
        for helper_id in helpers:
            stage = random.choices(
                config.LIFECYCLE_STAGES, weights=config.LIFECYCLE_WEIGHTS, k=1
            )[0]
            phase2_items.append(make_submission(
                assignment_id=a_id, requester_id=owner_id, helper_id=helper_id,
                title=title, description=description, subject=subject,
                difficulty=difficulty, assignment_created_at=created_at,
                target_status=stage,
            ))

    ok2, fail2 = batch_add_commit(phase2_items, "Phase2-Submissions")
    print(f"   ✅ {ok2} submissions created, {fail2} failed")

    # ════════════════════════════════════════════════════════════════════
    # PHASE 3 – Supporting notifications (sampled, bounded)
    # ════════════════════════════════════════════════════════════════════
    ok3 = ok4 = 0
    if with_extras:
        print(f"\n🔔 Phase 3: Generating notifications for a sample of submissions …")
        recent_subs = (
            HomeworkSubmission.query
            .filter(HomeworkSubmission.status != "pending")
            .order_by(HomeworkSubmission.created_at.desc())
            .limit(20000)
            .all()
        )
        sample_size = int(len(recent_subs) * config.NOTIFICATION_SAMPLE_RATE)
        sampled = random.sample(recent_subs, min(sample_size, len(recent_subs))) if recent_subs else []

        phase3_items = []
        for sub in sampled:
            if sub.status in ("submitted", "reviewed", "completed") and sub.submitted_at:
                phase3_items.append(make_notification(
                    user_id=sub.requester_id,
                    notification_type="homework_solution_submitted",
                    title="New solution for your assignment",
                    body=f"A helper submitted a solution for '{sub.title}'",
                    related_id=sub.assignment_id, created_at=sub.submitted_at,
                ))
            if sub.is_marked_helpful and sub.reaction_at:
                phase3_items.append(make_notification(
                    user_id=sub.helper_id,
                    notification_type="homework_marked_helpful",
                    title="Your help was marked helpful! 🎉",
                    body=f"Your solution for '{sub.title}' was rated '{sub.reaction_type}'",
                    related_id=sub.assignment_id, created_at=sub.reaction_at,
                ))

        ok3, fail3 = batch_add_commit(phase3_items, "Phase3-Notifications")
        print(f"   ✅ {ok3} notifications created, {fail3} failed")

        # ════════════════════════════════════════════════════════════════
        # PHASE 4 – Recent activity feed entries (must be <24h old to show)
        # ════════════════════════════════════════════════════════════════
        print(f"\n📡 Phase 4: Generating recent activity feed entries …")
        active_uids = [uid for uid in user_ids if adjacency.get(uid)]
        phase4_items = []
        for _ in range(min(config.ACTIVITY_TARGET_ROWS, len(active_uids) * 2)):
            uid = random.choice(active_uids) if active_uids else random.choice(user_ids)
            kind = random.choice(["assignment_created", "homework_shared", "homework_help_given"])
            data = {"subject": random.choice(SUBJECTS), "difficulty": pick_difficulty()}
            phase4_items.append(make_activity(uid, kind, data))

        ok4, fail4 = batch_add_commit(phase4_items, "Phase4-ActivityFeed")
        print(f"   ✅ {ok4} activity entries created, {fail4} failed")
    else:
        print("\n⏭️  Skipping notifications/activity feed (--no-extras)")

    validate_seed_integrity(user_ids)
    print_summary(len(population), tier_counts)
    return True

# ============================================================================
# VALIDATION
# ============================================================================

def validate_seed_integrity(user_ids: List[int]) -> bool:
    print("\n🔍 Validating seeded homework data …")
    ok = True
    id_set = set(user_ids)

    bad_owner = Assignment.query.filter(~Assignment.user_id.in_(id_set)).count()
    if bad_owner:
        print(f"   ❌ {bad_owner} assignments with an invalid owner")
        ok = False

    self_help = HomeworkSubmission.query.filter(
        HomeworkSubmission.requester_id == HomeworkSubmission.helper_id
    ).count()
    if self_help:
        print(f"   ❌ {self_help} submissions where requester == helper")
        ok = False

    orphan_subs = HomeworkSubmission.query.filter(
        ~HomeworkSubmission.assignment_id.in_(db.session.query(Assignment.id))
    ).count()
    if orphan_subs:
        print(f"   ❌ {orphan_subs} submissions pointing at a missing assignment")
        ok = False

    if ok:
        print("   ✅ No invalid owners, no self-help, no orphaned submissions")
    return ok

# ============================================================================
# SUMMARY
# ============================================================================

def print_summary(population_size: int, tier_counts: Counter):
    print("\n" + "=" * 68)
    print("📊 SEED SUMMARY")
    print("=" * 68)

    total_assignments = Assignment.query.count()
    total_shared = Assignment.query.filter_by(is_shared_for_help=True).count()
    total_submissions = HomeworkSubmission.query.count()

    print(f"\n👥 Population: {population_size} approved students")
    print(f"\n📚 Assignments: {total_assignments}")
    print(f"   Shared for help : {total_shared} ({total_shared / max(total_assignments,1) * 100:.1f}%)")
    print(f"   Private         : {total_assignments - total_shared}")

    print(f"\n🤝 Submissions: {total_submissions}")
    for stage in ["pending", "submitted", "reviewed", "completed"]:
        c = HomeworkSubmission.query.filter_by(status=stage).count()
        pct = c / max(total_submissions, 1) * 100
        print(f"   {stage:10s}: {c:6d} ({pct:.1f}%)")

    print(f"\n📈 Combined homework-feed records: {total_assignments + total_submissions}")

    print(f"\n⏰ Assignment status breakdown:")
    for status in ["not_started", "in_progress", "completed"]:
        c = Assignment.query.filter_by(status=status).count()
        print(f"   {status:12s}: {c}")

    overdue = Assignment.query.filter(
        Assignment.due_date < now(), Assignment.status != "completed"
    ).count()
    print(f"   overdue      : {overdue}")

    print(f"\n🎭 Usage tiers:")
    for name, _, drange in config.USAGE_TIERS:
        c = tier_counts.get(name, 0)
        print(f"   {name:8s} ({drange[0]:>2}-{drange[1]:<2}): {c} students")

    subj_counts = Counter(a.subject for a in Assignment.query.with_entities(Assignment.subject).all())
    print(f"\n📖 Top subjects:")
    for subj, cnt in subj_counts.most_common(8):
        print(f"   {subj:<20}: {cnt}")

    reaction_counts = Counter(
        r[0] for r in HomeworkSubmission.query
        .filter(HomeworkSubmission.reaction_type.isnot(None))
        .with_entities(HomeworkSubmission.reaction_type).all()
    )
    print(f"\n⭐ Reactions given:")
    for r in REACTION_TYPES:
        print(f"   {r:12s}: {reaction_counts.get(r, 0)}")

    notif_count = Notification.query.filter_by(notification_type="homework_solution_submitted").count() + \
                  Notification.query.filter_by(notification_type="homework_marked_helpful").count()
    activity_count = ActivityFeed.query.filter(
        ActivityFeed.activity_type.in_(["assignment_created", "homework_shared", "homework_help_given"])
    ).count()
    print(f"\n🔔 Homework-related notifications: {notif_count}")
    print(f"📡 Homework-related activity feed : {activity_count}")

    print("\n" + "=" * 68)
    print("✨ Homework feed seed complete! Ready for endpoint testing.")
    print("=" * 68 + "\n")

# ============================================================================
# ENTRY POINT
# ============================================================================

def parse_args():
    parser = argparse.ArgumentParser(description="Seed homework feed data across the full student population.")
    parser.add_argument("--clear", action="store_true", help="Clear existing homework data first, no prompt.")
    parser.add_argument("--dry-run", action="store_true", help="Preview counts, write nothing.")
    parser.add_argument("--no-extras", action="store_true", help="Skip notifications/activity feed generation.")
    parser.add_argument("--seed", type=int, default=None, help="Override the RNG seed.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    random.seed(args.seed if args.seed is not None else config.RANDOM_SEED)

    with app.app_context():
        if not args.dry_run:
            if not clear_homework_data(force=args.clear):
                sys.exit(1)

        success = seed_homework_feed(dry_run=args.dry_run, with_extras=not args.no_extras)

        if success:
            logger.info("Homework feed seed script completed successfully.")
            sys.exit(0)
        else:
            logger.error("Homework feed seed script failed.")
            sys.exit(1)
