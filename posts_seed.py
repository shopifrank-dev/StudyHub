"""
StudyHub Posts Feed Seed Script (Production-Grade)
====================================================
Generates a large, realistic, lived-in Posts feed across the ~3,000 students
created by seed_students.py.

Run AFTER seed_students.py (required) and connection_seed-1.py (recommended,
not a hard dependency — mentions/reactions just read more naturally with a
social graph already in place).

SCOPE
-----
This script seeds ONLY the Posts feature and its directly-related tables:
    Post, PostView, PostReaction, PostFollow,
    Comment, CommentLike, CommentHelpfulMark, Mention (post/comment only)

It intentionally does NOT touch:
    Bookmark, BookmarkFolder   (explicitly out of scope per product decision)
    Thread, ThreadMember, ThreadJoinRequest (explicitly out of scope)

DESIGN OVERVIEW
----------------
1. POSTING-ACTIVITY TIERS
   Every student is assigned a posting-activity tier (quiet / casual /
   average / active / influencer) via weighted random draw, each with its
   own target post-count range. Per-user targets are then proportionally
   scaled so the grand total lands on SeedConfig.NUM_POSTS. This produces
   the "some students post twice a semester, some post daily, a handful
   become known contributors" shape without hardcoding specific users.

2. CONTENT DIVERSITY VIA CATEGORIES
   ~20 content categories (study tips, questions, lecture notes, campus
   news, internships, career advice, exam prep, motivation, productivity,
   programming help, math, science, engineering, medicine, business, law,
   project showcases, event announcements, club activities, scholarships,
   research, personal achievements, graduation, study resources, book
   recommendations, campus memes) each carry their own title/body template
   pools and map onto the ONE of the five post_type values the schema
   actually supports (question / discussion / announcement / resource /
   problem — enforced elsewhere in the app, e.g. posts.py's valid_types
   checks). Category selection is mildly biased by the author's class
   level (first-years lean questions/exam-prep, final-years lean
   career/graduation/research), so the feed doesn't read as generic.

3. POPULARITY TIERS DRIVE ENGAGEMENT
   Each post independently draws a popularity tier (obscure / normal /
   notable / hot / viral). The draw is weighted by the AUTHOR's posting
   tier (influencers land in "hot"/"viral" far more often), which is what
   makes a handful of students look like genuine campus micro-celebrities.
   Views / reactions / comments are then sampled from ranges tied to that
   popularity tier, so popular posts organically attract more interaction.

4. TIMELINE
   Posts are spread across a 9-month window with a recency skew (more
   recent activity is more common, but the tail reaches back 9 months),
   avoiding both "everything happened yesterday" and uniform-random
   clustering artifacts.

5. PERFORMANCE
   - Zero N+1 queries: users + student profiles are batch-loaded once into
     in-memory dicts up front.
   - `sample_others()` draws small random samples directly from the user
     pool (O(k)) instead of rebuilding a filtered O(n) list per post.
   - High-volume, id-independent rows (views, reactions, comment likes,
     helpful marks, follows, mentions) are written with
     `db.session.bulk_save_objects()` in large batches instead of
     `session.add()` one at a time.
   - Posts/Comments still go through the ORM (`session.add` + a single
     `flush()` per micro-batch) since later steps need their generated
     ids — but flushes are batched, not per-row, wherever the ordering
     allows it.

Run standalone:
    python posts_seed.py
"""

import random
import datetime
import logging
from typing import List, Dict, Set, Tuple, Optional
from collections import Counter, defaultdict

from sqlalchemy.exc import SQLAlchemyError
from extensions import db
from models import (
    User, StudentProfile, Post, PostView, Comment, CommentLike,
    CommentHelpfulMark, PostReaction, PostFollow, Mention, PostEvent,
)

# ============================================================================
# CONFIGURATION
# ============================================================================

class SeedConfig:
    """Centralized configuration for the posts feed seed."""

    NUM_POSTS = 23_500                 # target, within the requested 22k-25k
    SEED_RANDOM_STATE = 42
    POST_BATCH_SIZE = 250              # posts flushed/committed per micro-batch
    BULK_BATCH_SIZE = 5_000            # size for bulk_save_objects flushes

    # ---- Posting-activity tiers: (name, weight, (min_posts, max_posts)) ----
    POST_COUNT_TIERS = [
        ("quiet",       30, (0, 2)),
        ("casual",      35, (3, 8)),
        ("average",     20, (9, 15)),
        ("active",      10, (16, 30)),
        ("influencer",  5,  (31, 60)),
    ]
    GUARANTEE_PRIMARY_ACTIVE = True
    PRIMARY_USER_ID = 1

    # ---- Popularity tiers: (name, views_range, reactions_range, comments_range) ----
    POPULARITY_TIERS = [
        ("obscure", (0, 3),   (0, 2),   (0, 1)),
        ("normal",  (2, 12),  (1, 8),   (0, 3)),
        ("notable", (10, 30), (5, 20),  (2, 6)),
        ("hot",     (25, 70), (15, 45), (4, 12)),
        ("viral",   (60, 150), (30, 90), (8, 25)),
    ]
    # Popularity-tier selection weights, indexed by author posting tier
    # (obscure, normal, notable, hot, viral)
    POPULARITY_WEIGHTS_BY_AUTHOR_TIER = {
        "quiet":       [70, 25, 4, 1, 0],
        "casual":      [50, 35, 12, 3, 0],
        "average":     [30, 40, 20, 8, 2],
        "active":      [15, 30, 30, 20, 5],
        "influencer":  [5, 15, 25, 35, 20],
    }

    # Fraction of posts that get at least one file attachment
    ATTACHMENT_RATE = 0.35
    MAX_ATTACHMENTS_PER_POST = 3
    COMMENT_ATTACHMENT_RATE = 0.10

    THREAD_ENABLED_RATE = 0.08   # flag only — no Thread rows are created
    PINNED_RATE = 0.015
    LOCKED_RATE = 0.01

    SOLVED_RATE = 0.55           # of question/problem posts

    REPLY_RATE = 0.35
    MAX_REPLIES_PER_COMMENT = 4
    COMMENT_LIKE_RATE = 0.10        # per-candidate-liker probability, scaled by popularity
    COMMENT_HELPFUL_RATE = 0.15     # only on question/problem posts

    FOLLOW_RATE = 0.12               # fraction of viewers who also follow

    POST_MENTION_RATE = 0.09
    COMMENT_MENTION_RATE = 0.06

    # ---- Timeline ----
    MAX_DAYS_AGO = 270    # ~9 months of feed history
    RECENCY_BIAS_EXPONENT = 1.6   # >1 skews toward more-recent days

config = SeedConfig()

# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('seed_posts.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================================================
# CONTENT POOLS
# ============================================================================

SUBJECTS = [
    "Calculus", "Linear Algebra", "Physics I", "Organic Chemistry",
    "Data Structures", "Algorithms", "Database Systems", "Operating Systems",
    "Web Development", "Machine Learning", "Statistics", "Discrete Math",
    "Computer Networks", "Software Engineering", "Computer Architecture",
    "Thermodynamics", "Circuit Theory", "Microeconomics", "Financial Accounting",
    "Constitutional Law", "Structural Analysis", "Differential Equations",
    "Anatomy", "Pharmacology", "Marketing Management", "Contract Law",
    "Organic Synthesis", "Fluid Mechanics", "Public Health Policy",
]

TOPICS = [
    "integration by parts", "eigenvalues", "Big-O notation", "normalization",
    "recursion", "binary trees", "SQL joins", "process scheduling",
    "gradient descent", "hypothesis testing", "Lagrange multipliers",
    "stress-strain curves", "Thevenin equivalents", "supply and demand",
    "balance sheets", "case briefs", "vector spaces", "Fourier series",
    "object-oriented design", "API design", "concurrency", "type systems",
    "cardiac cycle physiology", "drug metabolism", "tort liability",
    "reaction mechanisms", "bending moments", "epidemiological surveys",
]

DEPARTMENTS_FALLBACK = [
    "Computer Science", "Electrical Engineering", "Mechanical Engineering",
    "Civil Engineering", "Mathematics", "Physics", "Chemistry", "Biology",
    "Economics", "Accounting", "Business Administration", "Law",
    "Architecture", "Statistics", "Psychology", "Medicine & Surgery",
]

TAG_POOL = [
    "exam-prep", "midterm", "finals", "assignment", "project", "lab-report",
    "study-group", "deadline", "tutorial", "notes", "past-questions",
    "group-work", "research", "internship", "career", "coding", "math-help",
    "urgent", "revision", "thesis", "presentation", "scholarship", "campus-life",
    "networking", "motivation", "productivity", "book-club", "graduation",
]

COMPANIES = [
    "Google", "Microsoft", "MTN", "Flutterwave", "Paystack", "Andela",
    "Deloitte", "PwC", "Interswitch", "Konga", "Jumia", "GTBank",
    "Access Bank", "Zenith Bank", "Chevron", "Shell", "Accenture", "IBM",
    "Amazon", "a fast-growing local startup", "KPMG", "Union Bank",
]

CLUBS = [
    "Robotics Club", "Debate Society", "Entrepreneurship Club", "Coding Club",
    "Drama Society", "Red Cross Society", "Model United Nations",
    "Photography Club", "Chess Club", "Environmental Club", "Toastmasters",
    "Investment Club", "Literary & Debating Society", "Cultural Dance Troupe",
]

EVENTS = [
    "Career Fair", "Hackathon", "Cultural Day", "Career Talk", "Tech Summit",
    "Alumni Networking Night", "Sports Fiesta", "Job Fair",
    "Innovation Challenge", "Leadership Summit", "Research Symposium",
    "Freshers' Welcome Night",
]

BOOKS = [
    "Atomic Habits", "Deep Work", "The Lean Startup", "Sapiens",
    "Clean Code", "Rich Dad Poor Dad", "The Pragmatic Programmer",
    "Thinking, Fast and Slow", "Zero to One",
    "The 7 Habits of Highly Effective People", "Grit", "The Alchemist",
]

SCHOLARSHIPS = [
    "MTN Foundation Scholarship", "Chevening Scholarship", "PTDF Scholarship",
    "Mastercard Foundation Scholarship", "NNPC/Total Scholarship",
    "Federal Government Bursary", "Departmental Merit Award",
    "Dangote Foundation Scholarship",
]

# ============================================================================
# CONTENT CATEGORIES
# ============================================================================
# Each category maps to exactly one of the five schema-valid post_type
# values: question, discussion, announcement, resource, problem.
# `weight` is the base selection weight (before class-level bias).

CATEGORIES = [
    dict(
        key="question_general", post_type="question", weight=16,
        titles=[
            "Can someone explain {topic} in {subject}?",
            "Struggling with {subject} — {topic} doesn't make sense",
            "How do you approach {topic} problems in {subject}?",
            "Quick question about {subject}: {topic}",
            "Anyone understand {topic} from today's {subject} lecture?",
            "Need help with {topic} before the {subject} deadline",
        ],
        bodies=[
            "I've been stuck on {topic} for {subject} for a couple of hours. Went through the slides twice and it's still not clicking. Could someone break it down with a simple example?",
            "Working through the {subject} problem set and got stuck on {topic}. My approach gives a different answer than the textbook — where am I going wrong?",
            "Our lecturer moved fast through {topic} today and I couldn't keep up. Does anyone have notes or a simpler explanation for {subject}?",
            "I understand the theory behind {topic} but can't apply it to actual {subject} problems. Any tips on practice strategy?",
            "Is there a trick to remembering how {topic} works in {subject}? I keep mixing it up with a similar concept during tests.",
        ],
    ),
    dict(
        key="assignment_discussion", post_type="question", weight=10,
        titles=[
            "Anyone else confused about the {subject} assignment brief?",
            "{subject} assignment — is {topic} even in scope?",
            "How long is everyone's {subject} assignment turning out?",
            "Group assignment for {subject}: how are you splitting {topic}?",
        ],
        bodies=[
            "The {subject} assignment instructions feel vague around {topic}. Is anyone else interpreting it differently, or am I overthinking it?",
            "My group can't agree on how to divide the {subject} assignment. We're stuck specifically on the {topic} section — how would you split it?",
            "Just checking the class's general vibe on the {subject} assignment before I submit — does {topic} need a full derivation or just the final result?",
        ],
    ),
    dict(
        key="programming_help", post_type="problem", weight=13,
        titles=[
            "Can't get this {subject} problem to work out — {topic}",
            "Where's the bug in my {topic} solution? ({subject})",
            "{subject} problem: {topic} — output doesn't match expected",
            "Help debugging my approach to {topic} in {subject}",
        ],
        bodies=[
            "Attempted this {topic} problem for {subject} three different ways and keep getting a result that doesn't match the expected output. Attaching my code — what am I missing?",
            "This {subject} problem on {topic} seems straightforward but my solution keeps throwing an error I can't trace. Would appreciate a second pair of eyes.",
            "Posting my full working for this {topic} problem. Something's off in either my logic or my edge-case handling — can't tell which.",
        ],
    ),
    dict(
        key="math_science_problem", post_type="problem", weight=9,
        titles=[
            "Where did I go wrong on this {topic} question? ({subject})",
            "{subject} problem set — {topic} answer doesn't match the back of the book",
        ],
        bodies=[
            "Went through this {topic} problem for {subject} step by step and my final answer is off. Sharing my working, hoping someone can spot the mistake.",
            "Stuck between two methods for this {topic} problem in {subject}. Both feel valid but give different answers — which one is correct and why?",
        ],
    ),
    dict(
        key="study_tips", post_type="discussion", weight=10,
        titles=[
            "My study routine for {subject} — sharing in case it helps",
            "Best resources for self-studying {subject}?",
            "What's your study routine for {subject} like?",
            "Tips that actually helped me get through {subject}",
        ],
        bodies=[
            "I've tried a few different resources for {subject} and wanted to compare notes with everyone else taking it this semester.",
            "Sharing what worked for me revising {topic} for {subject} — spaced repetition plus past questions made a huge difference.",
            "Curious how people structure their {subject} revision. Sharing my rough weekly plan below, would appreciate feedback.",
        ],
    ),
    dict(
        key="exam_prep", post_type="discussion", weight=11,
        titles=[
            "How is everyone preparing for the {subject} finals?",
            "{subject} exam in a few days — anyone have a game plan?",
            "Panicking slightly about the {subject} exam, need a plan",
        ],
        bodies=[
            "With {subject} finals coming up, curious how people are structuring their revision. Sharing my plan below, would love feedback.",
            "Feeling behind on {topic} with the {subject} exam approaching. What's everyone's last-week strategy?",
            "Does anyone have a condensed summary for {subject}? Trying to consolidate before the exam and running low on time.",
        ],
    ),
    dict(
        key="lecture_notes", post_type="resource", weight=8,
        titles=[
            "Compiled notes for {subject} — {topic}",
            "Step-by-step guide to {topic} in {subject}",
            "Cheat sheet: {topic} formulas for {subject}",
        ],
        bodies=[
            "Made these notes while revising {topic} for {subject} and figured I'd share in case they help anyone else. Includes worked examples and common pitfalls.",
            "Summarized the key {topic} formulas and when to use each one. Hope this saves someone a few hours before the {subject} exam.",
            "Cleaned up my {subject} lecture notes on {topic} — should be a lot easier to follow than my scribbles from class.",
        ],
    ),
    dict(
        key="study_resources", post_type="resource", weight=7,
        titles=[
            "Free {subject} practice questions ({topic})",
            "Past exam questions for {subject}, organized by topic",
            "Dropping some {subject} resources here for anyone who needs them",
        ],
        bodies=[
            "Put together a small practice set covering {topic} for {subject}, answers included at the end — feel free to check your work.",
            "Found this really useful while preparing for {subject}, sharing here so it doesn't just sit in my drive forever.",
            "Collected a few years of {subject} past questions. Not official, but the patterns repeat more than you'd expect.",
        ],
    ),
    dict(
        key="project_showcase", post_type="resource", weight=6,
        titles=[
            "Just shipped a small {subject}-related side project",
            "Built something for my {subject} coursework, sharing here",
            "Final project for {subject} is done — feedback welcome",
        ],
        bodies=[
            "Spent the last few weeks building a project touching on {topic} for {subject}. Would love feedback from anyone who's tried something similar.",
            "Finished my {subject} project centered around {topic}. Learned a lot more from the debugging than the actual build, honestly.",
        ],
    ),
    dict(
        key="research_discussion", post_type="discussion", weight=6,
        titles=[
            "Anyone working on undergrad research around {topic}?",
            "Looking for a research partner interested in {subject}",
            "Thoughts on recent developments touching {topic}?",
        ],
        bodies=[
            "Been reading up on {topic} within {subject} and want to turn it into a small research project. Anyone interested in collaborating?",
            "Reflecting on {subject} and how it connects to real problems outside the classroom. Anyone have interesting examples from internships or research?",
        ],
    ),
    dict(
        key="internship_experience", post_type="discussion", weight=9,
        titles=[
            "Just finished my internship at {company} — happy to answer questions",
            "Internship search tips after applying to {company} and others",
            "What I learned interning at {company}",
        ],
        bodies=[
            "Wrapped up my internship at {company} last month. It taught me way more about {subject} in practice than a semester of lectures did — happy to answer questions if anyone's applying.",
            "Applied to a bunch of places including {company} for internships this cycle. Sharing what worked (and what clearly didn't) for anyone going through the same thing.",
            "Started an internship at {company} recently and it's been a steep but good learning curve, especially applying {subject} concepts to real problems.",
        ],
    ),
    dict(
        key="career_advice", post_type="discussion", weight=9,
        titles=[
            "Career advice for {subject} students — what actually matters?",
            "How relevant is {subject} coursework to actual jobs in the field?",
            "What do employers actually look for from {department} graduates?",
        ],
        bodies=[
            "Talked to a few alumni recently about breaking into the field after {department}. Sharing the recurring advice in case it helps other final-year students.",
            "Curious how much of {subject} coursework actually shows up in real jobs versus what employers say they care about in interviews.",
            "Been thinking about career paths after {department} and wanted to open this up — what's worked for people ahead of us?",
        ],
    ),
    dict(
        key="scholarship_announcement", post_type="announcement", weight=5,
        titles=[
            "{scholarship} applications now open",
            "Deadline reminder: {scholarship}",
            "New scholarship opportunity for {department} students",
        ],
        bodies=[
            "The {scholarship} is open for applications. Eligibility and deadlines are on the portal — worth applying even if you think it's a long shot.",
            "Reminder that the {scholarship} deadline is coming up soon. Don't sleep on this one, especially if you're in {department}.",
        ],
    ),
    dict(
        key="event_announcement", post_type="announcement", weight=7,
        titles=[
            "{event} happening this week — details inside",
            "Reminder: {event} is coming up",
            "Organizing a {subject} review session before finals",
        ],
        bodies=[
            "The {event} is happening this week. Should be worth attending if you're anywhere near {department} or just want to network.",
            "Putting together a review session covering the full {subject} syllabus before finals. Will share the agenda once it's finalized.",
            "Quick heads up that the {event} has been scheduled — mark your calendars, it's usually worth the time.",
        ],
    ),
    dict(
        key="club_activity", post_type="announcement", weight=5,
        titles=[
            "{club} meeting this week — new members welcome",
            "{club} recruiting for the new semester",
        ],
        bodies=[
            "The {club} is meeting again this week. Open to anyone curious, no prior experience needed — just show up.",
            "{club} is recruiting new members this semester. It's been one of the better decisions I've made outside of coursework, honestly recommend it.",
        ],
    ),
    dict(
        key="deadline_announcement", post_type="announcement", weight=5,
        titles=[
            "Reminder: {subject} assignment deadline moved",
            "Study group for {subject} starting this week",
            "New {subject} resources added to the shared drive",
        ],
        bodies=[
            "Heads up — the {subject} deadline has shifted. Double check the updated date so nobody gets caught out.",
            "Starting a small study group for {subject}, meeting twice a week. Open to anyone in the department who wants to join — drop a comment if interested.",
            "Added a bunch of new {subject} material to the shared resource pool. Worth a look before your next assignment.",
        ],
    ),
    dict(
        key="motivation_productivity", post_type="discussion", weight=8,
        titles=[
            "How do you stay motivated during {subject}-heavy weeks?",
            "Productivity system that's actually kept me sane this semester",
            "Feeling burnt out — how does everyone push through {subject}?",
        ],
        bodies=[
            "This semester's {subject} workload has been relentless. Curious what's actually keeping people motivated versus what just sounds good on paper.",
            "Started time-blocking my {subject} study sessions a few weeks ago and it's genuinely helped. Sharing in case it's useful to anyone else drowning right now.",
            "Not going to lie, {subject} has me questioning everything this week. If anyone has a system that works, please share.",
        ],
    ),
    dict(
        key="book_recommendation", post_type="discussion", weight=5,
        titles=[
            "Just finished {book} — recommend it for anyone in {department}",
            "Book recommendations for {department} students?",
        ],
        bodies=[
            "Finally finished reading {book}. More relevant to {department} than I expected going in — recommend it if you have the time.",
            "Looking for book recommendations relevant to {department}, ideally something outside the standard reading list. What's actually worth the time?",
        ],
    ),
    dict(
        key="personal_achievement", post_type="discussion", weight=6,
        titles=[
            "Small win: finally passed {subject}!",
            "Proud of myself for pushing through {subject} this semester",
        ],
        bodies=[
            "{subject} genuinely almost broke me this semester but I passed, and I just want to put that out there for anyone in the same boat right now — it's possible.",
            "Not a huge milestone in the grand scheme of things, but finishing {subject} feels like a real win after how rough this semester was.",
        ],
    ),
    dict(
        key="graduation_update", post_type="discussion", weight=4,
        titles=[
            "Officially done with {department} coursework — what a ride",
            "Final semester in {department}, feeling all kinds of emotions",
        ],
        bodies=[
            "Just submitted my last {department} assignment ever. Strange feeling looking back at how much has changed since first year.",
            "Down to my final semester in {department}. Equal parts excited and terrified about what comes next, if I'm honest.",
        ],
    ),
    dict(
        key="campus_meme", post_type="discussion", weight=6,
        titles=[
            "The state of {subject} lectures right now, no cap",
            "Me pretending I understand {topic} in {subject} class",
            "That feeling when the {subject} exam has zero relation to the lecture slides",
        ],
        bodies=[
            "Someone please explain how {subject} lectures go from 'basic recap' to 'derive this from first principles' with zero warning in between.",
            "Sat through the entire {subject} class nodding along to {topic} and understood absolutely nothing. We've all been there.",
            "The gap between what's taught in {subject} and what shows up on the exam continues to be the funniest ongoing joke on this campus.",
        ],
    ),
]

CATEGORY_BY_KEY = {c["key"]: c for c in CATEGORIES}

# Class-level content bias: multiplies category weight for students at
# these levels, so the feed reflects genuinely different concerns by year.
LEVEL_BOOST = {
    "junior": {  # 100 / 200 level
        "keys": {"question_general", "assignment_discussion", "exam_prep",
                 "motivation_productivity", "campus_meme", "study_tips"},
        "multiplier": 1.6,
    },
    "senior": {  # 400 / 500 level
        "keys": {"career_advice", "internship_experience", "research_discussion",
                 "graduation_update", "personal_achievement"},
        "multiplier": 1.9,
    },
}

def _level_bucket(level: str) -> str:
    if level in ("100 Level", "200 Level"):
        return "junior"
    if level in ("400 Level", "500 Level"):
        return "senior"
    return "mid"


# ---- Comment templates ------------------------------------------------------

COMMENT_TEMPLATES = [
    "This helped a lot, thank you!",
    "I was wondering the same thing — following for updates.",
    "Try breaking it down into smaller steps first, that's what worked for me.",
    "Pretty sure the issue is in how you're setting up the initial condition.",
    "Here's how I approached a similar problem: {topic} usually trips people up because of the sign convention.",
    "Can you share more of your working? Hard to tell without seeing the full steps.",
    "This is a great explanation, saving this for later.",
    "I think there's a small error around the second step — double check your substitution.",
    "We covered this in office hours last week, happy to share notes if useful.",
    "Same thing happened to me on the last assignment. Turned out to be a rounding issue.",
    "Not 100% sure but I believe the correct approach involves {topic}.",
    "Could you upload the full question? Might be missing some context.",
    "This is exactly what I needed before the exam, thanks for posting!",
    "I'd double check the assumptions you're making here.",
    "Great resource, bookmarking this for revision season.",
    "Have you tried approaching it from the other direction first?",
    "This matches what the lecturer said in class, good summary.",
    "Solid explanation. One thing I'd add — watch out for edge cases.",
    "Appreciate you sharing your work, makes it easier to spot the mistake.",
    "I ran into the exact same wall last semester, it gets easier with practice.",
    "Congrats, well deserved!",
    "This is such a relatable post honestly.",
    "Following this thread, need this too.",
    "Underrated post, more people need to see this.",
]

REPLY_TEMPLATES = [
    "Thanks, that makes sense now!",
    "Oh I see it now, appreciate the catch.",
    "Good point, I'll redo that part.",
    "Makes sense, thank you for clarifying.",
    "That's helpful, will try it that way.",
    "Ah okay, I was overcomplicating it.",
    "Got it, thanks for taking the time to explain.",
    "That fixed it for me too, thanks!",
    "Appreciate the quick reply!",
    "Will do, thanks again.",
    "Congrats again, super happy for you.",
]

# ============================================================================
# HELPER FUNCTIONS — TEXT GENERATION
# ============================================================================

def _fill_context(department: str) -> Dict[str, str]:
    """Build a context dict with every placeholder a template might use."""
    return {
        "subject": random.choice(SUBJECTS),
        "topic": random.choice(TOPICS),
        "department": department,
        "company": random.choice(COMPANIES),
        "club": random.choice(CLUBS),
        "event": random.choice(EVENTS),
        "book": random.choice(BOOKS),
        "scholarship": random.choice(SCHOLARSHIPS),
    }


def pick_category(level: str) -> dict:
    bucket = _level_bucket(level)
    boost = LEVEL_BOOST.get(bucket)
    keys = [c["key"] for c in CATEGORIES]
    weights = []
    for c in CATEGORIES:
        w = c["weight"]
        if boost and c["key"] in boost["keys"]:
            w = w * boost["multiplier"]
        weights.append(w)
    chosen_key = random.choices(keys, weights=weights, k=1)[0]
    return CATEGORY_BY_KEY[chosen_key]


def generate_post_content(category: dict, department: str) -> Tuple[str, str, List[str], str]:
    """Returns (title, body, tags, post_type) for a post in the given category."""
    ctx = _fill_context(department)
    title = random.choice(category["titles"]).format(**ctx)
    body = random.choice(category["bodies"]).format(**ctx)

    num_tags = random.choice([0, 1, 1, 2, 2, 3])
    tags = random.sample(TAG_POOL, num_tags) if num_tags else []

    return title, body, tags, category["post_type"]


def generate_comment_text(is_reply: bool, topic_hint: Optional[str] = None) -> str:
    pool = REPLY_TEMPLATES if is_reply else COMMENT_TEMPLATES
    template = random.choice(pool)
    if "{topic}" in template:
        return template.format(topic=topic_hint or random.choice(TOPICS))
    return template


# ============================================================================
# HELPER FUNCTIONS — ATTACHMENTS
# ============================================================================

IMAGE_EXTENSIONS = ["jpg", "jpeg", "png", "gif", "webp"]
DOCUMENT_EXTENSIONS = ["pdf", "docx", "pptx", "xlsx", "txt"]
VIDEO_EXTENSIONS = ["mp4", "mov"]

ATTACHMENT_FILENAME_STEMS = [
    "lecture_notes", "homework_scan", "practice_problems", "diagram",
    "worked_solution", "study_guide", "formula_sheet", "lab_report",
    "presentation_slides", "whiteboard_photo", "graph_output", "summary_notes",
    "exam_review", "code_snippet_screenshot", "data_table", "project_demo",
]

CLOUDINARY_BASE = "https://res.cloudinary.com/studyhub/upload/v1700000000"


def _ext_and_type() -> Tuple[str, str]:
    roll = random.random()
    if roll < 0.55:
        return random.choice(IMAGE_EXTENSIONS), "image"
    elif roll < 0.90:
        return random.choice(DOCUMENT_EXTENSIONS), "document"
    else:
        return random.choice(VIDEO_EXTENSIONS), "video"


def generate_attachment(seed_tag: str) -> Dict[str, str]:
    ext, res_type = _ext_and_type()
    stem = random.choice(ATTACHMENT_FILENAME_STEMS)
    unique = f"{seed_tag}_{random.randint(1000, 9999)}"
    filename = f"{stem}_{unique}.{ext}"
    folder = "images" if res_type == "image" else ("videos" if res_type == "video" else "documents")
    url = f"{CLOUDINARY_BASE}/{folder}/{filename}"
    return {"url": url, "type": res_type, "filename": filename}


def generate_attachments(seed_tag: str, max_count: int) -> List[Dict[str, str]]:
    count = random.randint(1, max_count)
    return [generate_attachment(f"{seed_tag}_{i}") for i in range(count)]


# ============================================================================
# HELPER FUNCTIONS — SAMPLING / DATES
# ============================================================================

def sample_others(pool: List, exclude_id: int, k: int) -> List:
    """O(k) random sample from `pool` excluding one id. Avoids rebuilding a
    filtered O(n) list per call — critical at this scale (23k+ posts)."""
    if k <= 0 or not pool:
        return []
    k = min(k, max(len(pool) - 1, 0))
    if k <= 0:
        return []
    sample = random.sample(pool, min(k + 1, len(pool)))
    sample = [u for u in sample if u.id != exclude_id]
    return sample[:k]


def random_past_datetime(max_days_ago: int) -> datetime.datetime:
    """Recency-skewed random timestamp within the last `max_days_ago` days."""
    r = random.random() ** config.RECENCY_BIAS_EXPONENT
    days_ago = int(r * max_days_ago)
    seconds_offset = random.randint(0, 86399)
    return (
        datetime.datetime.utcnow()
        - datetime.timedelta(days=days_ago)
        + datetime.timedelta(seconds=seconds_offset)
    )


def random_datetime_after(start: datetime.datetime, max_hours_later: int) -> datetime.datetime:
    hours_later = random.randint(0, max(1, max_hours_later))
    minutes_jitter = random.randint(0, 59)
    candidate = start + datetime.timedelta(hours=hours_later, minutes=minutes_jitter)
    now = datetime.datetime.utcnow()
    return min(candidate, now)


# ============================================================================
# POSTING-ACTIVITY TIER ASSIGNMENT
# ============================================================================

def assign_post_count_tiers(all_ids: List[int], primary_id: Optional[int]) -> Tuple[Dict[int, str], Dict[int, int]]:
    """Assign each user a tier + a raw target post count, then scale every
    target so the grand total lands on config.NUM_POSTS."""
    tier_map: Dict[int, str] = {}
    raw_targets: Dict[int, int] = {}

    names = [t[0] for t in config.POST_COUNT_TIERS]
    weights = [t[1] for t in config.POST_COUNT_TIERS]
    ranges = {t[0]: t[2] for t in config.POST_COUNT_TIERS}

    for uid in all_ids:
        if config.GUARANTEE_PRIMARY_ACTIVE and primary_id and uid == primary_id:
            tier = "active"
        else:
            tier = random.choices(names, weights=weights, k=1)[0]
        lo, hi = ranges[tier]
        raw_targets[uid] = random.randint(lo, hi)
        tier_map[uid] = tier

    raw_total = sum(raw_targets.values()) or 1
    scale = config.NUM_POSTS / raw_total

    scaled_targets: Dict[int, int] = {}
    running_total = 0
    for uid in all_ids:
        scaled = max(0, round(raw_targets[uid] * scale))
        scaled_targets[uid] = scaled
        running_total += scaled

    # Nudge the total to exactly match NUM_POSTS by adjusting random users
    diff = config.NUM_POSTS - running_total
    if diff != 0:
        adjustable = [uid for uid in all_ids if scaled_targets[uid] > 0 or diff > 0]
        random.shuffle(adjustable)
        i = 0
        while diff != 0 and adjustable:
            uid = adjustable[i % len(adjustable)]
            if diff > 0:
                scaled_targets[uid] += 1
                diff -= 1
            elif scaled_targets[uid] > 0:
                scaled_targets[uid] -= 1
                diff += 1
            i += 1
            if i > len(adjustable) * 3:
                break  # safety valve

    return tier_map, scaled_targets


def pick_popularity_tier(author_tier: str) -> Tuple[str, Tuple[int, int], Tuple[int, int], Tuple[int, int]]:
    names = [t[0] for t in config.POPULARITY_TIERS]
    weights = config.POPULARITY_WEIGHTS_BY_AUTHOR_TIER[author_tier]
    chosen = random.choices(names, weights=weights, k=1)[0]
    for t in config.POPULARITY_TIERS:
        if t[0] == chosen:
            return t
    return config.POPULARITY_TIERS[1]  # fallback: normal


# ============================================================================
# DATABASE PREREQUISITES
# ============================================================================

def verify_database_connection() -> bool:
    return True


def fetch_user_and_profile_data() -> Tuple[bool, Dict[int, User], Dict[int, dict]]:
    """Single batch load of users + profiles — no per-post queries later."""
    users = User.query.filter_by(status="approved").all()
    if len(users) < 3:
        logger.error(f"Insufficient users: found {len(users)}, need at least 3")
        print("❌ Error: Need at least 3 approved users to seed posts")
        print("💡 Tip: Run seed_students.py first")
        return False, {}, {}

    users_map = {u.id: u for u in users}

    profiles = StudentProfile.query.filter(
        StudentProfile.user_id.in_(list(users_map.keys()))
    ).all()
    profile_meta: Dict[int, dict] = {}
    for p in profiles:
        profile_meta[p.user_id] = {
            "department": p.department or random.choice(DEPARTMENTS_FALLBACK),
            "level": p.class_name or "200 Level",
        }
    for uid in users_map:
        if uid not in profile_meta:
            profile_meta[uid] = {
                "department": random.choice(DEPARTMENTS_FALLBACK),
                "level": "200 Level",
            }

    logger.info(f"Loaded {len(users_map)} users and {len(profile_meta)} profiles")
    print(f"✅ Found {len(users_map)} approved users with profile data")
    return True, users_map, profile_meta


def clear_existing_feed_data() -> bool:
    """Clear existing Posts-feature data. Bookmarks and Threads are NOT touched."""
    try:
        existing_count = Post.query.count()
        if existing_count > 0:
            logger.warning(f"Found {existing_count} existing posts")
            print(f"\n⚠️  Warning: {existing_count} posts already exist")
            response = input(
                "Clear all existing posts-feature data "
                "(posts, comments, reactions, views, follows, mentions)? "
                "Bookmarks/Threads will be left untouched. (yes/no): "
            )
            if response.lower() != "yes":
                logger.info("Seed aborted by user")
                print("❌ Seed aborted")
                return False

        print("🗑️  Clearing existing posts-feature data...")
        PostEvent.query.delete()
        Mention.query.filter(Mention.mentioned_in_type.in_(["post", "comment"])).delete(synchronize_session=False)
        PostFollow.query.delete()
        CommentHelpfulMark.query.delete()
        CommentLike.query.delete()
        PostReaction.query.delete()
        PostView.query.delete()
        Comment.query.delete()
        Post.query.delete()
        db.session.commit()
        print("✅ Cleared existing data")
        return True

    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Failed to clear existing data: {e}")
        print(f"❌ Failed to clear data: {e}")
        return False


# ============================================================================
# POST CREATION
# ============================================================================

def create_post_record(author_id: int, department: str, index: int) -> Post:
    level = _profile_meta[author_id]["level"]
    category = pick_category(level)
    title, body, tags, post_type = generate_post_content(category, department)

    posted_at = random_past_datetime(config.MAX_DAYS_AGO)

    resources = []
    if random.random() < config.ATTACHMENT_RATE:
        resources = generate_attachments(f"post{index}", config.MAX_ATTACHMENTS_PER_POST)

    thread_enabled = random.random() < config.THREAD_ENABLED_RATE
    is_pinned = random.random() < config.PINNED_RATE
    is_locked = random.random() < config.LOCKED_RATE

    is_solvable = post_type in ("question", "problem")
    is_solved = is_solvable and random.random() < config.SOLVED_RATE
    solved_at = random_datetime_after(posted_at, max_hours_later=240) if is_solved else None

    edited_at = random_datetime_after(posted_at, max_hours_later=72) if random.random() < 0.10 else None

    post = Post(
        student_id=author_id,
        title=title,
        text_content=body,
        post_type=post_type,
        resources=resources,
        department=department,
        tags=tags,
        positive_reactions_count=0,
        dislikes_count=0,
        views_count=0,
        comments_count=0,
        bookmark_count=0,             # bookmarks intentionally out of scope
        helpful_reactions_count=0,
        thread_enabled=thread_enabled,   # flag only, no Thread rows created
        is_solved=is_solved,
        is_pinned=is_pinned,
        is_locked=is_locked,
        posted_at=posted_at,
        edited_at=edited_at,
        solved_at=solved_at,
    )
    post._category_key = category["key"]  # transient, used only for stats
    return post


# ============================================================================
# MENTIONS
# ============================================================================

def maybe_create_mention(mentioned_in_type: str, mentioned_in_id: int, author_id: int,
                          candidate_pool: List[User], rate: float) -> Optional[Mention]:
    if random.random() >= rate:
        return None
    picked = sample_others(candidate_pool, author_id, 1)
    if not picked:
        return None
    mentioned_user = picked[0]
    return Mention(
        mentioned_in_type=mentioned_in_type,
        mentioned_in_id=mentioned_in_id,
        mentioned_user_id=mentioned_user.id,
        mentioned_by_user_id=author_id,
        is_read=random.choice([True, False]),
        mentioned_at=datetime.datetime.utcnow(),
    )


# ============================================================================
# COMMENT CREATION
# ============================================================================

def create_comment_record(post: Post, author_id: int, posted_at: datetime.datetime,
                           parent: Optional[Comment] = None) -> Comment:
    is_reply = parent is not None
    text = generate_comment_text(is_reply=is_reply)

    resources = []
    if random.random() < config.COMMENT_ATTACHMENT_RATE:
        tag = f"comment{post.id}_{author_id}_{random.randint(1, 99999)}"
        resources = generate_attachments(tag, max_count=1)

    is_solution = False
    if not is_reply and post.is_solved and post.post_type in ("question", "problem"):
        is_solution = random.random() < 0.15

    return Comment(
        post_id=post.id,
        student_id=author_id,
        parent_id=parent.id if parent else None,
        text_content=text,
        resources=resources,
        likes_count=0,
        helpful_count=0,
        replies_count=0,
        depth_level=1 if is_reply else 0,
        is_solution=is_solution,
        is_deleted=False,
        posted_at=posted_at,
    )


# ============================================================================
# MAIN SEED FUNCTION
# ============================================================================

_profile_meta: Dict[int, dict] = {}  # populated in seed_posts(), read by create_post_record


def seed_posts() -> bool:
    print("🌱 Starting StudyHub posts feed seed...")
    print(f"📝 Target: {config.NUM_POSTS} posts across the seeded student body\n")
    logger.info(f"Starting seed process for {config.NUM_POSTS} posts")

    random.seed(config.SEED_RANDOM_STATE)

    if not verify_database_connection():
        return False

    ok, users_map, profile_meta = fetch_user_and_profile_data()
    if not ok:
        return False

    global _profile_meta
    _profile_meta = profile_meta

    if not clear_existing_feed_data():
        return False

    all_ids = list(users_map.keys())
    all_users_list = list(users_map.values())
    primary_id = config.PRIMARY_USER_ID if config.PRIMARY_USER_ID in users_map else None

    print("🎭 Assigning posting-activity tiers...")
    tier_map, post_targets = assign_post_count_tiers(all_ids, primary_id)
    tier_counts = Counter(tier_map.values())
    for name in [t[0] for t in config.POST_COUNT_TIERS]:
        print(f"   {name}: {tier_counts.get(name, 0)} students")

    # ---- PHASE 1: POSTS -------------------------------------------------
    print(f"\n📝 Creating posts...")
    created_posts: List[Post] = []
    posts_created = 0
    posts_failed = 0
    category_counter: Counter = Counter()
    popularity_map: Dict[int, str] = {}  # post index (in created_posts) -> popularity tier name

    post_index = 0
    batch_buffer: List[Post] = []
    author_for_buffer: List[int] = []
    tier_for_buffer: List[str] = []

    order = all_ids[:]
    random.shuffle(order)

    def flush_post_batch():
        nonlocal batch_buffer, author_for_buffer, tier_for_buffer
        if not batch_buffer:
            return
        db.session.flush()  # populate ids for every post in this batch

        mentions_batch: List[Mention] = []
        for post_obj, author_id, author_tier in zip(batch_buffer, author_for_buffer, tier_for_buffer):
            mention = maybe_create_mention(
                "post", post_obj.id, author_id, all_users_list, config.POST_MENTION_RATE
            )
            if mention:
                mentions_batch.append(mention)

            pop_name, _, _, _ = pick_popularity_tier(author_tier)
            popularity_map[post_obj.id] = pop_name
            category_counter[post_obj._category_key] += 1

        if mentions_batch:
            db.session.bulk_save_objects(mentions_batch)

        db.session.commit()
        batch_buffer = []
        author_for_buffer = []
        tier_for_buffer = []

    for uid in order:
        target = post_targets.get(uid, 0)
        department = profile_meta[uid]["department"]
        author_tier = tier_map[uid]

        for _ in range(target):
            try:
                post = create_post_record(uid, department, post_index)
                post_index += 1
                db.session.add(post)
                created_posts.append(post)
                batch_buffer.append(post)
                author_for_buffer.append(uid)
                tier_for_buffer.append(author_tier)
                posts_created += 1

                if len(batch_buffer) >= config.POST_BATCH_SIZE:
                    flush_post_batch()
                    print(f"   ✓ Created {posts_created}/{config.NUM_POSTS} posts...")

            except Exception as e:
                db.session.rollback()
                logger.error(f"Error creating post for user {uid}: {e}")
                posts_failed += 1
                continue

    flush_post_batch()
    print(f"✅ Created {posts_created} posts ({posts_failed} failed)")
    logger.info(f"Posts phase complete: {posts_created} created, {posts_failed} failed")

    # ---- PHASE 2: VIEWS + FOLLOWS ---------------------------------------
    print(f"\n👀 Seeding post views and follows...")
    views_buffer: List[PostView] = []
    follows_buffer: List[PostFollow] = []
    views_created = 0
    follows_created = 0

    for post in created_posts:
        pop_name = popularity_map.get(post.id, "normal")
        pop_tier = next(t for t in config.POPULARITY_TIERS if t[0] == pop_name)
        views_lo, views_hi = pop_tier[1]

        num_viewers = random.randint(views_lo, views_hi)
        viewers = sample_others(all_users_list, post.student_id, num_viewers)
        if not viewers:
            continue

        for viewer in viewers:
            viewed_at = random_datetime_after(post.posted_at, max_hours_later=config.MAX_DAYS_AGO * 24)
            views_buffer.append(PostView(user_id=viewer.id, post_id=post.id, viewed_at=viewed_at))
            views_created += 1

            if random.random() < config.FOLLOW_RATE:
                follows_buffer.append(PostFollow(
                    post_id=post.id,
                    student_id=viewer.id,
                    followed_at=datetime.datetime.utcnow(),
                    notify_on_comment=random.choice([True, True, False]),
                    notify_on_solution=random.choice([True, True, False]),
                ))
                follows_created += 1

        post.views_count = len(viewers)

        if len(views_buffer) >= config.BULK_BATCH_SIZE:
            db.session.bulk_save_objects(views_buffer)
            views_buffer = []
        if len(follows_buffer) >= config.BULK_BATCH_SIZE:
            db.session.bulk_save_objects(follows_buffer)
            follows_buffer = []

    if views_buffer:
        db.session.bulk_save_objects(views_buffer)
    if follows_buffer:
        db.session.bulk_save_objects(follows_buffer)
    db.session.commit()
    print(f"✅ Created {views_created} post views and {follows_created} follows")
    logger.info(f"Views phase complete: {views_created} views, {follows_created} follows")

    # ---- PHASE 3: REACTIONS ---------------------------------------------
    print(f"\n❤️  Seeding post reactions...")
    REACTION_TYPES = ["like", "love", "helpful", "insightful", "fire", "wow", "celebrate"]
    REACTION_WEIGHTS = [0.35, 0.15, 0.20, 0.12, 0.08, 0.05, 0.05]

    reactions_buffer: List[PostReaction] = []
    reactions_created = 0

    for post in created_posts:
        pop_name = popularity_map.get(post.id, "normal")
        pop_tier = next(t for t in config.POPULARITY_TIERS if t[0] == pop_name)
        react_lo, react_hi = pop_tier[2]

        num_reactors = random.randint(react_lo, react_hi)
        reactors = sample_others(all_users_list, post.student_id, num_reactors)
        if not reactors:
            continue

        positive_count = 0
        helpful_count = 0
        for reactor in reactors:
            reaction_type = random.choices(REACTION_TYPES, weights=REACTION_WEIGHTS)[0]
            reactions_buffer.append(PostReaction(
                post_id=post.id,
                student_id=reactor.id,
                reaction_type=reaction_type,
                reacted_at=random_datetime_after(post.posted_at, max_hours_later=config.MAX_DAYS_AGO * 24),
            ))
            reactions_created += 1
            positive_count += 1
            if reaction_type == "helpful":
                helpful_count += 1

        post.positive_reactions_count = positive_count
        post.helpful_reactions_count = helpful_count

        if len(reactions_buffer) >= config.BULK_BATCH_SIZE:
            db.session.bulk_save_objects(reactions_buffer)
            reactions_buffer = []

    if reactions_buffer:
        db.session.bulk_save_objects(reactions_buffer)
    db.session.commit()
    print(f"✅ Created {reactions_created} post reactions")
    logger.info(f"Reactions phase complete: {reactions_created} reactions")

    # ---- PHASE 4: COMMENTS + REPLIES + LIKES + HELPFUL MARKS + MENTIONS --
    print(f"\n💬 Seeding comments and replies...")
    comments_created = 0
    replies_created = 0
    comment_likes_buffer: List[CommentLike] = []
    helpful_marks_buffer: List[CommentHelpfulMark] = []
    comment_mentions_buffer: List[Mention] = []
    comment_likes_created = 0
    helpful_marks_created = 0

    def flush_bulk_buffers(force=False):
        nonlocal comment_likes_buffer, helpful_marks_buffer, comment_mentions_buffer
        if force or len(comment_likes_buffer) >= config.BULK_BATCH_SIZE:
            if comment_likes_buffer:
                db.session.bulk_save_objects(comment_likes_buffer)
                comment_likes_buffer = []
        if force or len(helpful_marks_buffer) >= config.BULK_BATCH_SIZE:
            if helpful_marks_buffer:
                db.session.bulk_save_objects(helpful_marks_buffer)
                helpful_marks_buffer = []
        if force or len(comment_mentions_buffer) >= config.BULK_BATCH_SIZE:
            if comment_mentions_buffer:
                db.session.bulk_save_objects(comment_mentions_buffer)
                comment_mentions_buffer = []

    posts_processed = 0
    for post in created_posts:
        try:
            pop_name = popularity_map.get(post.id, "normal")
            pop_tier = next(t for t in config.POPULARITY_TIERS if t[0] == pop_name)
            comments_lo, comments_hi = pop_tier[3]
            num_top_level = random.randint(comments_lo, comments_hi)

            if num_top_level == 0:
                posts_processed += 1
                continue

            commenters = sample_others(all_users_list, post.student_id, num_top_level)
            if not commenters:
                posts_processed += 1
                continue

            top_level_comments: List[Comment] = []
            solution_assigned = False

            for commenter in commenters:
                commented_at = random_datetime_after(post.posted_at, max_hours_later=config.MAX_DAYS_AGO * 24)
                comment = create_comment_record(post, commenter.id, commented_at)

                if comment.is_solution:
                    if solution_assigned:
                        comment.is_solution = False
                    else:
                        solution_assigned = True

                db.session.add(comment)
                db.session.flush()  # need comment.id for replies/likes/mentions below
                top_level_comments.append(comment)
                comments_created += 1

                mention = maybe_create_mention(
                    "comment", comment.id, commenter.id, all_users_list, config.COMMENT_MENTION_RATE
                )
                if mention:
                    comment_mentions_buffer.append(mention)

                # Likes, scaled modestly by popularity via reaction pool size
                num_likers = int(config.COMMENT_LIKE_RATE * random.uniform(0.3, 2.0) * (comments_hi + 1))
                likers = sample_others(all_users_list, commenter.id, num_likers)
                for liker in likers:
                    comment_likes_buffer.append(CommentLike(
                        comment_id=comment.id,
                        student_id=liker.id,
                        liked_at=random_datetime_after(commented_at, max_hours_later=72),
                    ))
                    comment_likes_created += 1
                comment.likes_count = len(likers)

                # Helpful marks (question/problem posts only)
                helpful_count_for_comment = 0
                if post.post_type in ("question", "problem") and random.random() < config.COMMENT_HELPFUL_RATE:
                    num_helpful = random.randint(1, 4)
                    helpful_markers = sample_others(all_users_list, commenter.id, num_helpful)
                    for marker in helpful_markers:
                        helpful_marks_buffer.append(CommentHelpfulMark(
                            comment_id=comment.id,
                            user_id=marker.id,
                            marked_at=random_datetime_after(commented_at, max_hours_later=72),
                        ))
                        helpful_marks_created += 1
                    helpful_count_for_comment = len(helpful_markers)
                comment.helpful_count = helpful_count_for_comment

                # Replies (depth_level = 1 only — matches the app's enforced max depth)
                num_replies = 0
                if random.random() < config.REPLY_RATE:
                    num_replies = random.randint(1, config.MAX_REPLIES_PER_COMMENT)

                repliers = sample_others(all_users_list, -1, num_replies)  # any user, including author is fine for replies
                for replier in repliers:
                    replied_at = random_datetime_after(commented_at, max_hours_later=96)
                    reply = create_comment_record(post, replier.id, replied_at, parent=comment)
                    reply.is_solution = False
                    db.session.add(reply)
                    db.session.flush()
                    replies_created += 1

                    num_reply_likers = int(config.COMMENT_LIKE_RATE * random.uniform(0.1, 1.2) * (comments_hi + 1))
                    reply_likers = sample_others(all_users_list, replier.id, num_reply_likers)
                    for liker in reply_likers:
                        comment_likes_buffer.append(CommentLike(
                            comment_id=reply.id,
                            student_id=liker.id,
                            liked_at=random_datetime_after(replied_at, max_hours_later=48),
                        ))
                        comment_likes_created += 1
                    reply.likes_count = len(reply_likers)

                comment.replies_count = len(repliers)

            post.comments_count = len(top_level_comments) + sum(c.replies_count for c in top_level_comments)

            flush_bulk_buffers()
            posts_processed += 1

            if posts_processed % config.POST_BATCH_SIZE == 0:
                db.session.commit()
                print(f"   ✓ Processed comments for {posts_processed}/{len(created_posts)} posts "
                      f"({comments_created} comments, {replies_created} replies so far)...")

        except Exception as e:
            db.session.rollback()
            logger.error(f"Error seeding comments for post {post.id}: {e}")
            continue

    flush_bulk_buffers(force=True)
    db.session.commit()
    print(f"✅ Created {comments_created} top-level comments + {replies_created} replies")
    print(f"✅ Created {comment_likes_created} comment likes, {helpful_marks_created} helpful marks")
    logger.info(
        f"Comments phase complete: {comments_created} comments, {replies_created} replies, "
        f"{comment_likes_created} likes, {helpful_marks_created} helpful marks"
    )

    # ---- FINAL COMMIT -----------------------------------------------------
    try:
        db.session.commit()
        logger.info("Final commit successful")
    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Final commit failed: {e}")
        print(f"❌ Final commit failed: {e}")
        return False

    print_summary_statistics(tier_map, post_targets, category_counter, popularity_map)
    return True


# ============================================================================
# VALIDATION
# ============================================================================

def validate_seed_integrity() -> bool:
    print("\n🔍 Validating seeded posts data...")
    ok = True

    valid_types = {"question", "discussion", "announcement", "resource", "problem"}
    bad_type = Post.query.filter(~Post.post_type.in_(list(valid_types))).count()
    if bad_type:
        print(f"   ❌ {bad_type} posts with invalid post_type")
        ok = False

    valid_user_ids = {u.id for u in User.query.with_entities(User.id).all()}
    posts_bad_author = sum(
        1 for (sid,) in Post.query.with_entities(Post.student_id).all()
        if sid not in valid_user_ids
    )
    if posts_bad_author:
        print(f"   ❌ {posts_bad_author} posts reference an invalid author")
        ok = False

    orphan_comments = sum(
        1 for (pid,) in Comment.query.with_entities(Comment.post_id).all()
        if pid not in {p.id for p in Post.query.with_entities(Post.id).all()}
    )
    if orphan_comments:
        print(f"   ❌ {orphan_comments} comments reference a missing post")
        ok = False

    negative_counts = Post.query.filter(
        (Post.comments_count < 0) | (Post.positive_reactions_count < 0) | (Post.views_count < 0)
    ).count()
    if negative_counts:
        print(f"   ❌ {negative_counts} posts have negative denormalized counts")
        ok = False

    if ok:
        print("   ✅ All post_types valid, all foreign keys valid, no negative counts")
    return ok


# ============================================================================
# SUMMARY STATISTICS
# ============================================================================

def print_summary_statistics(tier_map, post_targets, category_counter, popularity_map):
    print("\n" + "=" * 60)
    print("📊 POSTS FEED SEED SUMMARY")
    print("=" * 60)

    total_posts = Post.query.count()
    total_comments = Comment.query.filter(Comment.depth_level == 0).count()
    total_replies = Comment.query.filter(Comment.depth_level == 1).count()
    total_views = PostView.query.count()
    total_reactions = PostReaction.query.count()
    total_comment_likes = CommentLike.query.count()
    total_helpful_marks = CommentHelpfulMark.query.count()
    total_follows = PostFollow.query.count()
    total_mentions = Mention.query.count()

    posts_with_attachments = sum(1 for (r,) in Post.query.with_entities(Post.resources).all() if r)

    print(f"Total Posts:          {total_posts}")
    print(f"  ├─ with attachments: {posts_with_attachments} "
          f"({(posts_with_attachments/total_posts*100 if total_posts else 0):.1f}%)")
    print(f"  ├─ solved:           {Post.query.filter_by(is_solved=True).count()}")
    print(f"  ├─ pinned:           {Post.query.filter_by(is_pinned=True).count()}")
    print(f"  └─ locked:           {Post.query.filter_by(is_locked=True).count()}")

    print(f"\nTotal Comments (top-level): {total_comments}")
    print(f"Total Replies (depth 1):    {total_replies}")
    print(f"Total Post Views:           {total_views}")
    print(f"Total Post Reactions:       {total_reactions}")
    print(f"Total Comment Likes:        {total_comment_likes}")
    print(f"Total Helpful Marks:        {total_helpful_marks}")
    print(f"Total Post Follows:         {total_follows}")
    print(f"Total Mentions:             {total_mentions}")

    print(f"\n🎭 Posting-Activity Tiers:")
    tier_counts = Counter(tier_map.values())
    for name, _, drange in config.POST_COUNT_TIERS:
        count = tier_counts.get(name, 0)
        pct = (count / max(len(tier_map), 1)) * 100
        print(f"   {name} ({drange[0]}-{drange[1]} target posts): {count} students ({pct:.1f}%)")

    top_users = sorted(post_targets.items(), key=lambda x: x[1], reverse=True)[:10]
    if top_users:
        top_ids = [uid for uid, _ in top_users]
        users_map = {u.id: u for u in User.query.filter(User.id.in_(top_ids)).all()}
        print(f"\n🌟 Top 10 Most Prolific Posters (by target):")
        for uid, target in top_users:
            user = users_map.get(uid)
            name = user.name if user else f"User#{uid}"
            actual = Post.query.filter_by(student_id=uid).count()
            print(f"   {name}: {actual} posts")

    print(f"\n📋 Post Type Distribution:")
    for ptype in ["question", "discussion", "announcement", "resource", "problem"]:
        count = Post.query.filter_by(post_type=ptype).count()
        pct = (count / total_posts * 100) if total_posts else 0
        print(f"  {ptype.capitalize():14s}: {count} ({pct:.1f}%)")

    print(f"\n🗂️  Top Content Categories:")
    for key, count in category_counter.most_common(10):
        pct = (count / total_posts * 100) if total_posts else 0
        print(f"  {key:24s}: {count} ({pct:.1f}%)")

    print(f"\n🔥 Popularity Tier Distribution:")
    pop_counts = Counter(popularity_map.values())
    for name, *_ in config.POPULARITY_TIERS:
        count = pop_counts.get(name, 0)
        pct = (count / total_posts * 100) if total_posts else 0
        print(f"  {name:10s}: {count} ({pct:.1f}%)")

    print(f"\n📂 Department Distribution (top 5):")
    dept_counts: Dict[str, int] = {}
    for (dept,) in Post.query.with_entities(Post.department).all():
        dept_counts[dept] = dept_counts.get(dept, 0) + 1
    for dept, count in sorted(dept_counts.items(), key=lambda x: -x[1])[:5]:
        print(f"  {dept:35s}: {count}")

    validate_seed_integrity()

    print("\n" + "=" * 60)
    print("✨ Posts feed seed complete! (Bookmarks and Threads intentionally skipped)")
    print("=" * 60 + "\n")

    logger.info("Summary statistics printed successfully")


# ============================================================================
# STANDALONE EXECUTION
# ============================================================================

if __name__ == "__main__":
    from app import app

    with app.app_context():
        success = seed_posts()
        if success:
            logger.info("Posts seed script completed successfully")
            exit(0)
        else:
            logger.error("Posts seed script failed")
            exit(1)
