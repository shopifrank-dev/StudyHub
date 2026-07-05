from app import create_app
from extensions import db

app, scheduler = create_app()

with app.app_context():
    from models import User, StudentProfile
    
    # Get first 10 students with StudentProfile
    students = (
        db.session.query(User)
        .join(StudentProfile, StudentProfile.user_id == User.id)
        .filter(User.status == "approved")
        .limit(10)
        .all()
    )
    
    print("\n" + "=" * 60)
    print("FIRST 10 STUDENTS - USERNAME & EMAIL")
    print("=" * 60)
    
    for i, student in enumerate(students, 1):
        print(f"{i:2}. {student.username:20} {student.email}")
    
    print("=" * 60)
    print(f"Total students shown: {len(students)}")
    print("=" * 60)