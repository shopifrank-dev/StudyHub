"""
StudyHub Student Seed Script (Production-Grade)
Creates ~3,000 realistic, diverse student accounts to make the platform
feel like an active university community from day one.

Architecture follows the existing user_seed.py pattern:
- SeedConfig for centralized settings
- Logging to file + console
- Batched inserts with periodic commits
- Helper functions for uniqueness (usernames/emails)
- Post-seed validation + summary statistics

Run standalone:
    python seed_students.py
"""

import random
import datetime
import logging
from typing import List, Dict, Set, Tuple
from collections import Counter

from werkzeug.security import generate_password_hash
from sqlalchemy.exc import SQLAlchemyError
from extensions import db
from models import User, StudentProfile, OnboardingDetails, AIUsageQuota

# ============================================================================
# CONFIGURATION
# ============================================================================

class SeedConfig:
    NUM_STUDENTS = 3000
    SEED_RANDOM_STATE = 42
    BATCH_SIZE = 200
    DEFAULT_PASSWORD = "password123"

    # Account age spread — up to ~2 years so the community looks established,
    # not freshly created.
    MAX_DAYS_AGO = 730
    MIN_DAYS_AGO = 1

    # Activity distribution
    ACTIVE_USER_PERCENTAGE = 0.72
    MAX_RECENT_ACTIVITY_DAYS = 14
    MAX_INACTIVE_DAYS = 180

config = SeedConfig()

# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('seed_students.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================================================
# NAME POOLS
# ============================================================================
# Nigerian names dominate (majority), split across major ethnic groups.
# English, other-African, and international names fill the remainder.

NIGERIAN_NAMES = {
    "yoruba": {
        "male": ["Oluwaseun", "Adewale", "Babajide", "Oluwafemi", "Adebayo", "Tunde",
                 "Olumide", "Ayodele", "Kolawole", "Damilare", "Oluwatobi", "Segun",
                 "Adekunle", "Gbenga", "Temitope", "Rotimi", "Oladipo", "Wale",
                 "Ifeoluwa", "Akintunde"],
        "female": ["Adaeze", "Folasade", "Temitayo", "Adunni", "Oluwabukola", "Yetunde",
                   "Bimpe", "Aduke", "Ronke", "Omolara", "Ayomide", "Oluwakemi",
                   "Simisola", "Bolanle", "Adebimpe", "Ifeoma", "Titilayo", "Modupe",
                   "Damilola", "Oyindamola"],
        "surnames": ["Adeyemi", "Ogundimu", "Afolabi", "Oyelaran", "Adebayo", "Fashola",
                     "Ogunleye", "Adewusi", "Oyewole", "Bankole", "Akinola", "Oyedepo",
                     "Falana", "Ojo", "Sowande", "Owolabi", "Adeoye", "Fagbenle"]
    },
    "igbo": {
        "male": ["Chukwuemeka", "Ikechukwu", "Chinedu", "Obinna", "Emeka", "Chidi",
                  "Kelechi", "Nnamdi", "Uchechukwu", "Chibuzor", "Ifeanyi", "Somtochukwu",
                  "Ekene", "Chukwudi", "Onyekachi", "Ugochukwu", "Kenechukwu", "Chimaobi"],
        "female": ["Chiamaka", "Ngozi", "Adaeze", "Chinyere", "Uchechi", "Ifeoma",
                    "Chiedu", "Obiageli", "Nkechi", "Amarachi", "Chinelo", "Adaobi",
                    "Ozioma", "Uzoamaka", "Chidinma", "Ebele", "Nneka", "Somtochukwu"],
        "surnames": ["Okafor", "Eze", "Nwosu", "Okoye", "Uzoma", "Anyanwu", "Chukwu",
                      "Nwachukwu", "Okonkwo", "Ibekwe", "Onwuka", "Nnaji", "Obi",
                      "Iheanacho", "Madu", "Okeke", "Ezenwa", "Nwankwo"]
    },
    "hausa": {
        "male": ["Abubakar", "Ibrahim", "Musa", "Sani", "Aliyu", "Yakubu", "Garba",
                  "Usman", "Nuhu", "Bello", "Suleiman", "Auwal", "Lawal", "Isa",
                  "Salisu", "Danjuma", "Ahmadu", "Faruk"],
        "female": ["Amina", "Fatima", "Hauwa", "Zainab", "Halima", "Aisha", "Maryam",
                    "Safiya", "Rabi", "Zuwaira", "Hadiza", "Balkisu", "Ummi", "Ladi",
                    "Rukayya", "Jamila", "Asma", "Khadija"],
        "surnames": ["Abdullahi", "Mohammed", "Ibrahim", "Sani", "Yusuf", "Bello",
                      "Garba", "Idris", "Suleiman", "Aliyu", "Musa", "Yakubu",
                      "Umar", "Danladi", "Adamu"]
    },
    "efik": {
        "male": ["Effiong", "Okon", "Edet", "Etim", "Ekpo", "Asuquo", "Ubong", "Ememobong"],
        "female": ["Ekaette", "Ememobong", "Uduak", "Idara", "Mfon", "Emem", "Enobong", "Iberedem"],
        "surnames": ["Ekpo", "Effiong", "Etim", "Udoh", "Bassey", "Umoren", "Asuquo", "Okpo"]
    },
    "tiv": {
        "male": ["Terhemba", "Iortyer", "Iyorwuese", "Dooshima", "Terver", "Igbawua", "Orkuma"],
        "female": ["Dooshima", "Mtsem", "Nguveren", "Doosuur", "Msuega", "Kwaghtser", "Anmbeve"],
        "surnames": ["Ityavyar", "Ugbah", "Iortyom", "Aondoakaa", "Gbaden", "Kwaghgba", "Achineku"]
    },
    "edo": {
        "male": ["Osaretin", "Osagie", "Iyobosa", "Uyi", "Aigbe", "Efosa", "Omoregie"],
        "female": ["Osasere", "Iyobosa", "Osamudiamen", "Efosa", "Aisosa", "Eghosa", "Omosede"],
        "surnames": ["Osayande", "Igbinovia", "Ehigiator", "Osayomwanbo", "Uwadia", "Aigbokhan"]
    },
    "urhobo": {
        "male": ["Oghenevwogaga", "Efe", "Ejiro", "Ovie", "Onome", "Emiko", "Oghenerukevwe"],
        "female": ["Oghenekaro", "Ejiro", "Onome", "Efemena", "Arero", "Ese", "Oghenetega"],
        "surnames": ["Emofe", "Okumagba", "Erhabor", "Oyibo", "Otobrise", "Egbagbe"]
    },
    "ijaw": {
        "male": ["Preye", "Tamuno", "Diepiriye", "Ibinabo", "Sotonye", "Ebimobowei"],
        "female": ["Ibiere", "Ebiere", "Tamaraebi", "Preye", "Sotonye", "Ibitamuno"],
        "surnames": ["Amachree", "Diri", "Fubara", "Wodi", "Okoroba", "Alamieyeseigha"]
    },
}
NIGERIAN_ETHNIC_WEIGHTS = {
    "yoruba": 26, "igbo": 25, "hausa": 20, "edo": 7,
    "urhobo": 6, "ijaw": 6, "efik": 5, "tiv": 5
}

ENGLISH_NAMES = {
    "male": ["James", "William", "Benjamin", "Henry", "Thomas", "Oliver", "Daniel",
              "Samuel", "Joseph", "Edward", "George", "Charles", "Michael", "Andrew"],
    "female": ["Charlotte", "Amelia", "Olivia", "Emily", "Grace", "Sophie", "Isabella",
                "Eleanor", "Alice", "Florence", "Victoria", "Hannah", "Rebecca", "Lucy"],
    "surnames": ["Smith", "Taylor", "Brown", "Wilson", "Evans", "Thomas", "Roberts",
                  "Johnson", "Walker", "Wright", "Robinson", "Clarke", "Bennett", "Hughes"]
}

OTHER_AFRICAN_NAMES = {
    "male": ["Kwame", "Kofi", "Sipho", "Thabo", "Kagiso", "Abebe", "Kwabena", "Tendai",
              "Mandla", "Jabari"],
    "female": ["Ama", "Akosua", "Nomvula", "Lindiwe", "Zanele", "Amara", "Wanjiru",
                "Chiara", "Nia", "Abena"],
    "surnames": ["Mensah", "Owusu", "Dlamini", "Mokoena", "Kariuki", "Haile", "Banda",
                  "Chikwanha", "Osei", "Mwangi"]
}

INTERNATIONAL_NAMES = {
    "male": ["Arjun", "Wei", "Ahmed", "Karim", "Liam", "Diego", "Hiro", "Rafael", "Omar"],
    "female": ["Priya", "Mei", "Fatima", "Layla", "Sofia", "Valentina", "Yuki", "Nadia"],
    "surnames": ["Sharma", "Chen", "Al-Farsi", "Hassan", "Garcia", "Nakamura", "Silva",
                  "Khan", "Patel"]
}

ORIGIN_WEIGHTS = {"nigerian": 68, "english": 14, "other_african": 11, "international": 7}

# ============================================================================
# DEPARTMENTS
# ============================================================================
# Provided frontend department list, expanded slightly with additional
# realistic faculties commonly found alongside it.

DEPARTMENTS = [
    "Accounting", "African / Nigerian Languages", "Adult Education & Extra-Mural Studies",
    "Agricultural Economics", "Agricultural Economics & Agribusiness",
    "Agricultural Extension & Rural Development", "Agronomy", "Anatomy / Physiology",
    "Animal Science", "Architecture", "Banking & Finance", "Biochemistry",
    "Biological Sciences / Biology", "Botany", "Building / Construction",
    "Business Administration / Management", "Chemical Engineering", "Chemistry",
    "Cooperative & Rural Development", "Computer Engineering", "Computer Science",
    "Curriculum & Instructional Technology", "Economics", "Electrical / Electronic Engineering",
    "Education & Biology", "Education & Chemistry", "Education & English Language",
    "Education & Geography", "Education & History", "Education & Mathematics",
    "Environmental Science", "Entrepreneurship", "Estate Management",
    "Fine & Applied Arts (Creative Arts)", "Food Science & Technology",
    "Forestry & Wildlife Management", "Geography", "Geology", "Geomatics / Surveying",
    "Guidance & Counselling", "Health & Physical Education", "Hospitality & Tourism",
    "Hospitality & Tourism Management", "Human Resources Management",
    "Industrial Relations & Personnel Management", "Insurance", "Law (Common / Civil Law)",
    "Library & Information Science", "Logistics & Supply Chain Management",
    "Mass Communication / Communication & Language Arts", "Marketing",
    "Marine / Environmental / Structural Engineering", "Mathematics",
    "Medical Biochemistry / Microbiology", "Medicine & Surgery", "Microbiology",
    "Metallurgical & Materials Engineering", "Nursing Science",
    "Office & Information Management", "Petroleum / Gas Engineering",
    "Pharmacy / Pharmaceutical Sciences", "Philosophy", "Project Management",
    "Public Health", "Public Administration", "Quantity Surveying",
    "Religious Studies / Theology", "Science Education",
    "Soil & Land Resources Management", "Soil Science", "Special Education",
    "Statistics", "Theatre Arts", "Transport Management", "Urban & Regional Planning",
    "Vocational / Technical Education", "Web Development", "Zoology",
    # Reasonable additions that fit naturally alongside the above list
    "Civil Engineering", "Mechanical Engineering", "International Relations",
    "Political Science", "Sociology", "Psychology", "Cyber Security",
    "Data Science", "Software Engineering", "Music",
]

# Popularity tiers so distribution isn't even — some departments have far
# more students than others, as in a real university.
POPULAR_DEPARTMENTS = {
    "Computer Science", "Business Administration / Management", "Accounting",
    "Law (Common / Civil Law)", "Medicine & Surgery", "Economics",
    "Mass Communication / Communication & Language Arts", "Banking & Finance",
    "Nursing Science", "Electrical / Electronic Engineering", "Public Administration",
    "Microbiology", "Political Science", "Psychology",
}
MODERATE_DEPARTMENTS = {
    "Biochemistry", "Pharmacy / Pharmaceutical Sciences", "Civil Engineering",
    "Mechanical Engineering", "Marketing", "Human Resources Management",
    "Architecture", "Estate Management", "Sociology", "Software Engineering",
    "Data Science", "Cyber Security", "Mathematics", "Physiology",
    "Anatomy / Physiology", "Statistics", "International Relations",
    "Environmental Science", "Chemical Engineering",
}

def _department_weight(dept: str) -> int:
    if dept in POPULAR_DEPARTMENTS:
        return 6
    if dept in MODERATE_DEPARTMENTS:
        return 3
    return 1

DEPARTMENT_WEIGHTS = [_department_weight(d) for d in DEPARTMENTS]

# Nigerian-style academic levels. Weighted so lower levels (bigger intake
# cohorts, natural attrition upward) are slightly more common.
CLASS_LEVELS = ["100 Level", "200 Level", "300 Level", "400 Level", "500 Level"]
CLASS_LEVEL_WEIGHTS = [28, 24, 22, 18, 8]

LEARNING_STYLES = [
    "Visual learner - I learn best with diagrams and charts",
    "Auditory learner - I prefer listening and discussion",
    "Kinesthetic learner - I learn by doing and practice",
    "Reading/Writing - I prefer written materials and notes",
]

STUDY_PREFERENCES = [
    "Morning study sessions", "Evening study sessions", "Group study",
    "One-on-one tutoring", "Video tutorials", "Practice problems",
    "Library sessions", "Weekend crash sessions",
]

INTERESTS_POOL = [
    "football", "music production", "photography", "debate", "entrepreneurship",
    "coding side-projects", "fashion design", "content creation", "chess",
    "volunteering", "public speaking", "reading novels", "fitness & gym",
    "cooking", "traveling", "graphic design", "poetry", "dance", "gaming",
    "campus fellowship", "student politics", "table tennis", "basketball",
]

BIO_TEMPLATES = [
    "{level} {department} student. Into {interest1} and always down to talk {subject}.",
    "Studying {department}, currently in {level}. Big on {interest1} outside class.",
    "{department} student trying to survive {level} 😅. Also really into {interest1}.",
    "Passionate about {subject} — happy to team up with fellow {department} students.",
    "{level} student in {department}. Free time goes to {interest1} and {interest2}.",
    "Here to connect with course mates and study smarter, not harder. {department}, {level}.",
    "Balancing {department} coursework with {interest1}. Open to study groups!",
]

# ============================================================================
# HELPERS
# ============================================================================

def _weighted_choice(items: List[str], weights: List[int]) -> str:
    return random.choices(items, weights=weights, k=1)[0]


def generate_name() -> Tuple[str, str, str]:
    """Returns (first_name, last_name, origin_tag) with realistic distribution."""
    origin = random.choices(
        list(ORIGIN_WEIGHTS.keys()), weights=list(ORIGIN_WEIGHTS.values()), k=1
    )[0]
    gender = random.choice(["male", "female"])

    if origin == "nigerian":
        ethnicity = random.choices(
            list(NIGERIAN_ETHNIC_WEIGHTS.keys()),
            weights=list(NIGERIAN_ETHNIC_WEIGHTS.values()), k=1
        )[0]
        pool = NIGERIAN_NAMES[ethnicity]
        first = random.choice(pool[gender])
        last = random.choice(pool["surnames"])
        # Occasionally mix ethnic surname with a different-ethnicity first name,
        # or add a hyphenated Christian/Muslim second first name — reflects
        # real-world Nigerian naming variety.
        if random.random() < 0.12:
            other = random.choice([e for e in NIGERIAN_NAMES if e != ethnicity])
            last = random.choice(NIGERIAN_NAMES[other]["surnames"])
        if random.random() < 0.15:
            extra_pool = random.choice(list(NIGERIAN_NAMES.values()))
            first = f"{first}-{random.choice(extra_pool[gender])}"
        return first, last, f"nigerian_{ethnicity}"

    pool = {
        "english": ENGLISH_NAMES,
        "other_african": OTHER_AFRICAN_NAMES,
        "international": INTERNATIONAL_NAMES,
    }[origin]
    first = random.choice(pool[gender])
    last = random.choice(pool["surnames"])
    return first, last, origin


def generate_username(first_name: str, last_name: str, used: Set[str]) -> str:
    f = first_name.lower().replace(" ", "").replace("-", "")
    l = last_name.lower().replace(" ", "").replace("-", "")
    styles = [
        f"{f}.{l}",
        f"{f}_{l}",
        f"{f}{l}",
        f"{f[0]}{l}",
        f"{f}.{l[0]}",
        f"{f}{random.randint(10, 99)}",
    ]
    random.shuffle(styles)
    for candidate in styles:
        if candidate not in used:
            used.add(candidate)
            return candidate
    # Fallback: append counter
    base = styles[0]
    counter = 1
    candidate = f"{base}{counter}"
    while candidate in used:
        counter += 1
        candidate = f"{base}{counter}"
    used.add(candidate)
    return candidate


def generate_email(username: str, used: Set[str]) -> str:
    domains = ["gmail.com", "yahoo.com", "outlook.com", "student.studyhub.edu.ng", "icloud.com"]
    random.shuffle(domains)
    for domain in domains:
        email = f"{username}@{domain}"
        if email not in used:
            used.add(email)
            return email
    counter = 1
    while True:
        email = f"{username}{counter}@{random.choice(domains)}"
        if email not in used:
            used.add(email)
            return email
        counter += 1


def generate_subjects(department: str) -> List[str]:
    """Generic but department-aware subject list (works across all 80+ depts
    without needing a hand-curated table for each one)."""
    templates = [
        f"Introduction to {department}",
        f"{department} Fundamentals",
        f"Advanced {department}",
        f"Research Methods in {department}",
        f"Seminar in {department}",
        "Statistics", "Communication Skills", "Entrepreneurship Studies",
    ]
    num = random.randint(3, 5)
    return random.sample(templates, num)


def generate_bio(department: str, level: str, subjects: List[str], interests: List[str]) -> str:
    template = random.choice(BIO_TEMPLATES)
    return template.format(
        department=department,
        level=level,
        subject=subjects[0] if subjects else "coursework",
        interest1=interests[0] if interests else "campus life",
        interest2=interests[1] if len(interests) > 1 else "music",
    )


def generate_reputation() -> int:
    return random.choices(
        [random.randint(0, 50), random.randint(51, 200),
         random.randint(201, 500), random.randint(501, 1000)],
        weights=[45, 30, 18, 7]
    )[0]


def generate_study_schedule() -> Dict[str, List[str]]:
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    time_slots = ["morning", "afternoon", "evening"]
    schedule = {}
    for day in random.sample(days, random.randint(2, 6)):
        schedule[day] = random.sample(time_slots, random.randint(1, 3))
    return schedule


def generate_activity_dates(joined_at: datetime.datetime) -> Tuple[datetime.datetime, int]:
    now = datetime.datetime.utcnow()
    days_since_join = (now - joined_at).days

    if random.random() < config.ACTIVE_USER_PERCENTAGE:
        last_active_days = random.randint(0, config.MAX_RECENT_ACTIVITY_DAYS)
    else:
        last_active_days = random.randint(15, config.MAX_INACTIVE_DAYS)

    last_active = now - datetime.timedelta(days=min(last_active_days, days_since_join) or 0)
    login_streak = random.randint(0, min(days_since_join, 30)) if last_active_days < 3 else 0
    return last_active, login_streak


# ============================================================================
# CORE RECORD CREATION
# ============================================================================

def create_random_student(
    used_usernames: Set[str], used_emails: Set[str]
) -> Tuple[User, StudentProfile, OnboardingDetails, AIUsageQuota, str]:
    first_name, last_name, origin = generate_name()
    username = generate_username(first_name, last_name, used_usernames)
    email = generate_email(username, used_emails)
    full_name = f"{first_name} {last_name}"
    pin = generate_password_hash(config.DEFAULT_PASSWORD)

    department = _weighted_choice(DEPARTMENTS, DEPARTMENT_WEIGHTS)
    class_level = _weighted_choice(CLASS_LEVELS, CLASS_LEVEL_WEIGHTS)

    subjects = generate_subjects(department)
    split = max(1, len(subjects) // 2)
    strong_subjects = subjects[:split]
    help_subjects = subjects[split:] or [subjects[0]]

    interests = random.sample(INTERESTS_POOL, random.randint(2, 4))
    bio = generate_bio(department, class_level, subjects, interests)
    reputation = generate_reputation()

    now = datetime.datetime.utcnow()
    days_ago = random.randint(config.MIN_DAYS_AGO, config.MAX_DAYS_AGO)
    joined_at = now - datetime.timedelta(days=days_ago)
    last_active, login_streak = generate_activity_dates(joined_at)
    study_schedule = generate_study_schedule()

    user = User(
        username=username,
        email=email,
        pin=pin,
        name=full_name,
        bio=bio,
        role="student",
        status="approved",
        email_verified=True,
        reputation=reputation,
        last_active=last_active,
        login_streak=login_streak,
        total_posts=random.randint(0, 60),
        total_helpful=random.randint(0, 25),
        skills=random.sample(strong_subjects, min(2, len(strong_subjects))),
        learning_goals=random.sample(help_subjects, min(2, len(help_subjects))),
        study_schedule=study_schedule,
        joined_at=joined_at,
        last_login=last_active,
    )
    user.update_reputation_level()

    student_profile = StudentProfile(
        user=user,
        pin=pin,
        username=username,
        full_name=full_name,
        department=department,
        class_name=class_level,
        status="active",
        registered_at=joined_at,
    )

    onboarding = OnboardingDetails(
        user=user,
        email=email,
        department=department,
        class_level=class_level,
        subjects=subjects,
        learning_style=random.choice(LEARNING_STYLES),
        study_preferences=random.sample(STUDY_PREFERENCES, random.randint(2, 4)),
        help_subjects=help_subjects,
        strong_subjects=strong_subjects,
        study_schedule=study_schedule,
        session_length=random.choice(["30-60 min", "1-2 hours", "2+ hours"]),
        last_updated=joined_at,
    )

    ai_quota = AIUsageQuota(
        user=user,
        daily_messages_limit=50,
        daily_messages_used=random.randint(0, 20),
        last_reset_date=datetime.date.today(),
        last_message_time=last_active,
    )

    return user, student_profile, onboarding, ai_quota, origin


# ============================================================================
# DATABASE OPERATIONS
# ============================================================================

def verify_database_connection() -> bool:
    return True


def clear_existing_data() -> bool:
    try:
        existing_count = User.query.count()
        if existing_count > 0:
            logger.warning(f"Found {existing_count} existing users")
            print(f"\n⚠️  Warning: {existing_count} users already exist")
            response = input("Clear all existing user data? (yes/no): ")
            if response.lower() != "yes":
                logger.info("Seed aborted by user")
                print("❌ Seed aborted")
                return False

        print("🗑️  Clearing existing data...")
        AIUsageQuota.query.delete()
        OnboardingDetails.query.delete()
        StudentProfile.query.delete()
        User.query.delete()
        db.session.commit()
        print("✅ Cleared existing data")
        return True
    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Failed to clear data: {e}")
        print(f"❌ Failed to clear data: {e}")
        return False


def seed_students() -> bool:
    print("🌱 Starting StudyHub student seed...")
    print(f"📝 Target: {config.NUM_STUDENTS} students\n")

    random.seed(config.SEED_RANDOM_STATE)

    if not verify_database_connection():
        return False
    if not clear_existing_data():
        return False

    used_usernames: Set[str] = set()
    used_emails: Set[str] = set()
    origin_counter: Counter = Counter()
    users_created = 0

    try:
        for i in range(config.NUM_STUDENTS):
            try:
                user, profile, onboarding, quota, origin = create_random_student(
                    used_usernames, used_emails
                )
                db.session.add(user)
                db.session.add(profile)
                db.session.add(onboarding)
                db.session.add(quota)
                origin_counter[origin] += 1
                users_created += 1

                if users_created % config.BATCH_SIZE == 0:
                    db.session.commit()
                    print(f"   ✓ Created {users_created}/{config.NUM_STUDENTS} students...")
            except Exception as e:
                logger.error(f"Error creating student {i}: {e}")
                db.session.rollback()
                continue

        db.session.commit()
        print(f"\n✅ Created {users_created} students successfully!")
        print_summary_statistics(origin_counter)
        return True

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        db.session.rollback()
        print(f"❌ Unexpected error: {e}")
        return False


# ============================================================================
# VALIDATION
# ============================================================================

def validate_seed_integrity() -> bool:
    """Post-seed sanity checks."""
    print("\n🔍 Validating seeded data...")
    ok = True

    usernames = [u.username for u in User.query.all()]
    emails = [u.email for u in User.query.all()]

    if len(usernames) != len(set(usernames)):
        print("   ❌ Duplicate usernames found")
        ok = False
    if len(emails) != len(set(emails)):
        print("   ❌ Duplicate emails found")
        ok = False

    valid_depts = set(DEPARTMENTS)
    bad_dept = StudentProfile.query.filter(
        ~StudentProfile.department.in_(valid_depts)
    ).count()
    if bad_dept:
        print(f"   ❌ {bad_dept} profiles with invalid department")
        ok = False

    orphans = User.query.outerjoin(StudentProfile).filter(
        StudentProfile.id.is_(None)
    ).count()
    if orphans:
        print(f"   ❌ {orphans} users missing a StudentProfile")
        ok = False

    if ok:
        print("   ✅ No duplicates, all departments valid, no orphaned records")
    return ok


# ============================================================================
# SUMMARY STATISTICS
# ============================================================================

def print_summary_statistics(origin_counter: Counter):
    print("\n" + "=" * 60)
    print("📊 SEED SUMMARY")
    print("=" * 60)

    total_users = User.query.count()
    print(f"Total Students: {total_users}")

    print("\n🌍 Name Origin Distribution:")
    for origin, count in origin_counter.most_common():
        pct = (count / max(total_users, 1)) * 100
        print(f"   {origin}: {count} ({pct:.1f}%)")

    print("\n📚 Top Departments:")
    dept_counts = Counter(p.department for p in StudentProfile.query.all())
    for dept, count in dept_counts.most_common(10):
        pct = (count / max(total_users, 1)) * 100
        print(f"   {dept}: {count} ({pct:.1f}%)")

    print("\n🎓 Class Level Distribution:")
    level_counts = Counter(p.class_name for p in StudentProfile.query.all())
    for level, count in level_counts.most_common():
        pct = (count / max(total_users, 1)) * 100
        print(f"   {level}: {count} ({pct:.1f}%)")

    complete_profiles = db.session.query(User).join(
        StudentProfile, StudentProfile.user_id == User.id
    ).join(
        OnboardingDetails, OnboardingDetails.user_id == User.id
    ).filter(User.status == "approved").count()

    print(f"\n✅ Data Completeness: {complete_profiles}/{total_users} complete profiles")

    validate_seed_integrity()

    print("\n" + "=" * 60)
    print("✨ Seed complete!")
    print("=" * 60 + "\n")


# ============================================================================
# STANDALONE EXECUTION
# ============================================================================

if __name__ == "__main__":
    from app import app

    with app.app_context():
        success = seed_students()
        if success:
            logger.info("Seed script completed successfully")
            exit(0)
        else:
            logger.error("Seed script failed")
            exit(1)
