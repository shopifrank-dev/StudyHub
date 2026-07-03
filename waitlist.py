"""
Waitlist with DEBUG prints to see what's happening
"""
from flask import Blueprint, request, jsonify, current_app
from extensions import db
from models import WaitlistSignup
from utils import send_waitlist_welcome_email, send_referral_milestone_email
import random
import string
import json
import requests
import os
from datetime import datetime, timedelta, date

stats_cache = {'data': None, 'expires_at': None}
waitlist_bp = Blueprint('waitlist', __name__, url_prefix='/api/waitlist')


def generate_referral_code():
    """Generate unique referral code"""
    max_attempts = 10
    for _ in range(max_attempts):
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        if not WaitlistSignup.query.filter_by(referral_code=code).first():
            return code
    timestamp = str(int(datetime.utcnow().timestamp()))[-4:]
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=4)) + timestamp


def calculate_tier_benefits(position):
    """Calculate tier benefits"""
    if position <= 100:
        return {
            "tier": "🥇 Founder Tier",
            "benefits": [
                "Lifetime Premium Access FREE",
                "Exclusive Founder Badge",
                "Day 1 Early Access",
                "VIP Discord Channel",
                "Direct Line to Founders",
                "Shape Product Roadmap"
            ]
        }
    elif position <= 500:
        return {
            "tier": "🥈 Early Adopter Tier",
            "benefits": [
                "1 Year Premium FREE",
                "Early Adopter Badge",
                "Week 1 Access",
                "Priority Support",
                "Beta Feature Access"
            ]
        }
    elif position <= 2000:
        return {
            "tier": "🥉 Early Access Tier",
            "benefits": [
                "3 Months Premium FREE",
                "Early Access Badge",
                "Week 2 Access",
                "Community Founder Status"
            ]
        }
    else:
        return {
            "tier": "📚 Community Tier",
            "benefits": [
                "1 Month Premium FREE",
                "Early Member Badge",
                "Week 3 Access"
            ]
        }


def get_app_url():
    """Get app URL"""
    url = current_app.config.get('CURRENT_URL', os.environ.get('CURRENT_URL', 'http://127.0.0.1:5001/'))
    if not url.endswith('/'):
        url += '/'
    return url


@waitlist_bp.route("/join", methods=["POST"])
def join_waitlist():
    """Join the waitlist"""
    
    print("\n" + "="*70)
    print("🎯 JOIN WAITLIST ROUTE CALLED")
    print("="*70)
    
    try:
        data = request.get_json()
        print(f"Request data: {data}")
        
        if not data:
            return jsonify({"status": "error", "message": "Invalid request"}), 400
        
        email = data.get("email", "").strip().lower()
        referred_by = data.get("ref", "").strip().upper()
        
        print(f"Email: {email}")
        print(f"Referred by: {referred_by or 'None'}")
        
        if not email or '@' not in email:
            return jsonify({"status": "error", "message": "Invalid email"}), 400
        
        # Check existing
        existing = WaitlistSignup.query.filter_by(email=email).first()
        if existing:
            print(f"⚠️  User already exists: {email}")
            tier_info = calculate_tier_benefits(existing.waitlist_position)
            return jsonify({
                "status": "success",
                "message": "You're already on the waitlist!",
                "data": {
                    "position": existing.waitlist_position,
                    "referral_code": existing.referral_code,
                    "referral_count": existing.referral_count,
                    "referral_link": f"{get_app_url()}?ref={existing.referral_code}",
                    "tier": tier_info["tier"],
                    "benefits": tier_info["benefits"]
                }
            }), 200
        
        # Create new signup
        print("Creating new signup...")
        referral_code = generate_referral_code()
        position = WaitlistSignup.query.count() + 51
        
        signup = WaitlistSignup(
            email=email,
            referral_code=referral_code,
            referred_by=referred_by if referred_by else None,
            waitlist_position=position
        )
        db.session.add(signup)
        
        tier_info = calculate_tier_benefits(position)
        print(f"Position: {position}")
        print(f"Tier: {tier_info['tier']}")
        
        # Handle referrer
        if referred_by:
            print(f"Looking for referrer: {referred_by}")
            referrer = WaitlistSignup.query.filter_by(referral_code=referred_by).first()
            if referrer:
                print(f"Found referrer: {referrer.email}")
                referrer.referral_count += 1
                new_count = referrer.referral_count
                
                if new_count >= 20:
                    referrer.waitlist_position = min(referrer.waitlist_position, 50)
                elif new_count >= 10:
                    referrer.waitlist_position = min(referrer.waitlist_position, 100)
                elif new_count % 3 == 0:
                    referrer.waitlist_position = max(1, referrer.waitlist_position - 100)
                
                db.session.commit()
                print(f"Referrer updated: {new_count} referrals, position {referrer.waitlist_position}")
                
                if new_count in [3, 10, 20]:
                    print(f"\n🎯 MILESTONE REACHED: {new_count} referrals")
                    print(f"Calling send_referral_milestone_email...")
                    try:
                        result = send_referral_milestone_email(
                            referrer.email,
                            new_count,
                            referrer.waitlist_position,
                            referrer.referral_code
                        )
                        print(f"Milestone email result: {result}")
                    except Exception as e:
                        print(f"❌ Milestone email exception: {e}")
                        import traceback
                        traceback.print_exc()
            else:
                print(f"⚠️  Referrer not found: {referred_by}")
        else:
            db.session.commit()
            print("No referrer, committed signup")
        
        # SEND WELCOME EMAIL
        print("\n" + "="*70)
        print("📧 ABOUT TO SEND WELCOME EMAIL")
        print("="*70)
        print(f"To: {email}")
        print(f"Position: {position}")
        print(f"Referral code: {referral_code}")
        print("Calling send_waitlist_welcome_email()...")
        
        email_sent = False
        try:
            email_sent = send_waitlist_welcome_email(
                email,
                referral_code,
                position,
                tier_info
            )
            print(f"\nEmail send result: {email_sent}")
            
            if email_sent:
                print("✅ EMAIL WAS SENT SUCCESSFULLY!")
            else:
                print("❌ EMAIL SEND RETURNED FALSE!")
                
        except Exception as e:
            print(f"❌ EXCEPTION IN EMAIL SEND: {e}")
            import traceback
            traceback.print_exc()
        
        print("="*70 + "\n")
        
        # Return response
        response = {
            "status": "success",
            "message": "🎉 You're on the list!",
            "data": {
                "position": position,
                "referral_code": referral_code,
                "referral_link": f"{get_app_url()}?ref={referral_code}",
                "tier": tier_info["tier"],
                "benefits": tier_info["benefits"],
                "email_sent": email_sent
            }
        }
        
        print(f"Returning response: {response['status']}")
        return jsonify(response), 201
        
    except Exception as e:
        db.session.rollback()
        print(f"\n❌ EXCEPTION IN JOIN ROUTE: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "status": "error",
            "message": f"Failed: {str(e)}"
        }), 500


@waitlist_bp.route("/test-email", methods=["POST"])
def test_email():
    """Test email - SUPER SIMPLE"""
    
    print("\n" + "="*70)
    print("🧪 TEST EMAIL ROUTE CALLED")
    print("="*70)
    
    try:
        data = request.get_json()
        test_email = data.get("email")
        
        print(f"Test email address: {test_email}")
        
        if not test_email:
            return jsonify({"status": "error", "message": "Email required"}), 400
        
        print("\nImporting send_email_now from utils...")
        from utils import send_email_now
        print("✅ Import successful")
        
        print(f"\nCalling send_email_now({test_email})...")
        
        html = """
        <html>
            <body style="font-family: Arial; padding: 40px; text-align: center;">
                <h1 style="color: #667eea;">✅ Test Successful!</h1>
                <p>Your email configuration is working.</p>
            </body>
        </html>
        """
        
        result = send_email_now(test_email, "🧪 StudyHub Test", html)
        
        print(f"\nTest email result: {result}")
        
        if result:
            print("✅ TEST EMAIL SENT!")
            return jsonify({
                "status": "success",
                "message": f"✅ Test email sent to {test_email}!",
                "config": {
                    "server": current_app.config.get('MAIL_SERVER'),
                    "port": current_app.config.get('MAIL_PORT'),
                    "username": current_app.config.get('MAIL_USERNAME')
                }
            }), 200
        else:
            print("❌ TEST EMAIL FAILED!")
            return jsonify({
                "status": "error",
                "message": "Email failed. Check server logs."
            }), 500
            
    except Exception as e:
        print(f"\n❌ EXCEPTION IN TEST EMAIL: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "status": "error",
            "message": f"Error: {str(e)}"
        }), 500


@waitlist_bp.route("/stats", methods=["GET"])
def get_stats():
    """Get stats"""
    global stats_cache
    if stats_cache['data'] and stats_cache['expires_at'] > datetime.utcnow():
        return jsonify(stats_cache['data'])
    
    try:
        total = WaitlistSignup.query.count() + 50
        today = date.today()
        today_count = WaitlistSignup.query.filter(
            db.func.date(WaitlistSignup.signup_date) == today
        ).count()
        
        result = {
            "status": "success",
            "data": {
                "total_signups": total,
                "signups_today": today_count,
                "spots_remaining": max(0, 10000 - total)
            }
        }
        
        stats_cache['data'] = result
        stats_cache['expires_at'] = datetime.utcnow() + timedelta(seconds=30)
        
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@waitlist_bp.route("/check/<email>", methods=["GET"])
def check_status(email):
    """Check status"""
    try:
        signup = WaitlistSignup.query.filter_by(email=email.lower().strip()).first()
        if not signup:
            return jsonify({"status": "error", "message": "Not found"}), 404
        
        tier_info = calculate_tier_benefits(signup.waitlist_position)
        return jsonify({
            "status": "success",
            "data": {
                "position": signup.waitlist_position,
                "referral_code": signup.referral_code,
                "referral_count": signup.referral_count,
                "referral_link": f"{get_app_url()}?ref={signup.referral_code}",
                "tier": tier_info["tier"],
                "benefits": tier_info["benefits"],
                "signup_date": signup.signup_date.isoformat()
            }
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@waitlist_bp.route("/recent", methods=["GET"])
def recent_signups():
    """Recent signups"""
    try:
        records = WaitlistSignup.query.order_by(
            WaitlistSignup.signup_date.desc()
        ).limit(10).all()
        
        data = []
        for r in records:
            email_parts = r.email.split("@")
            name = email_parts[0].title() if email_parts else "Anonymous"
            data.append({"name": name, "time": r.signup_date.isoformat()})
        
        if not data:
            data = [{"name": "Alex", "time": datetime.utcnow().isoformat()}]
        
        return jsonify({"status": "success", "data": data})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@waitlist_bp.route("/leaderboard", methods=["GET"])
def leaderboard():
    """Leaderboard"""
    try:
        total = WaitlistSignup.query.count() + 50
        top_referrers = WaitlistSignup.query.filter(
            WaitlistSignup.referral_count > 0
        ).order_by(
            WaitlistSignup.referral_count.desc(),
            WaitlistSignup.waitlist_position.asc()
        ).limit(10).all()
        
        data = []
        for idx, signup in enumerate(top_referrers, 1):
            email_parts = signup.email.split("@")
            name = email_parts[0][:3] + "***" if email_parts else "User"
            data.append({
                "rank": idx,
                "name": name,
                "referral_count": signup.referral_count,
                "position": signup.waitlist_position,
                "total": total
            })
        
        return jsonify({"status": "success", "data": data})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500