# app.py - PRODUCTION READY VERSION WITH FLASK-MAIL + APScheduler
# ============================================================================
# IMPORTANT: Load environment variables FIRST before any other imports
# ============================================================================
from dotenv import load_dotenv

from flask import Flask, render_template, request, jsonify
from flask_migrate import Migrate
from werkzeug.middleware.proxy_fix import ProxyFix
from services.websocket_messages import init_message_websocket
from services.websocket_threads import thread_ws_manager
from extensions import db, mail
import os
from routes.student.helpers import (
    token_required, success_response, error_response
)

from waitlist import waitlist_bp
import logging
from routes.student import student_bp
from routes.student.auth import google_bp
from logging.handlers import RotatingFileHandler
load_dotenv()


os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"


# ============================================================================
# Configuration Class
# ============================================================================
migrate = Migrate()


class Config:
    """Production-ready configuration"""
    
    # Flask Core
    SECRET_KEY = os.environ.get('SECRET_KEY')
    if not SECRET_KEY:
        raise ValueError("SECRET_KEY environment variable is not set!")
    
    FLASK_ENV = os.environ.get('FLASK_ENV', 'production')
    DEBUG = False  # Always False in production
    TESTING = False
    
    # Database
    DATABASE_URL = os.environ.get('DATABASE_NEW_URL')
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is not set!")
    
    # Fix for Heroku/Railway postgres:// vs postgresql://
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
    
    SQLALCHEMY_DATABASE_URI = DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # Flask-Mail Configuration (Gmail with App Password)
    MAIL_SERVER = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
    MAIL_PORT = int(os.environ.get('MAIL_PORT', 587))
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS', 'True').lower() == 'true'
    MAIL_USE_SSL = os.environ.get('MAIL_USE_SSL', 'False').lower() == 'true'
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER')
    
    # Email reliability settings for production
    MAIL_MAX_EMAILS = 50
    MAIL_TIMEOUT = 5
    MAIL_DEBUG = False
    
    # Suppress SSL warnings in production
    MAIL_SUPPRESS_SEND = False
    MAIL_ASCII_ATTACHMENTS = False
    
    if not MAIL_USERNAME or not MAIL_PASSWORD:
        print("⚠️  WARNING: Email credentials not configured!")
    else:
        print(f"✅ Email configured: {MAIL_USERNAME}")
    
    # Application Settings
    CURRENT_URL = os.environ.get('CURRENT_URL', 'http://127.0.0.1:5001/')
    UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', 'static/upload')
    
    # Mailchimp (Optional)
    MAILCHIMP_API_KEY = os.environ.get('MAILCHIMP_API_KEY')
    MAILCHIMP_LIST_ID = os.environ.get('MAILCHIMP_LIST_ID')
    
    # Security Settings
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max upload
    JSON_SORT_KEYS = False
    JSONIFY_PRETTYPRINT_REGULAR = False
    
    # Session Security
    SESSION_COOKIE_SECURE = os.environ.get('SESSION_COOKIE_SECURE', 'False').lower() == 'true'
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    PERMANENT_SESSION_LIFETIME = 3600  # 1 hour

    # FIX: was hardcoded to string "name" which broke Learnora.
    # Now reads from env as int; defaults to 0 (disabled) if not set.
    LEARNORA_BOT_USER_ID = int(os.environ.get('LEARNORA_BOT_USER_ID', 0))

    # ── Scheduler ─────────────────────────────────────────────────────────────
    # Set SCHEDULER_ENABLED=false in .env to disable the scheduler entirely
    # (useful in staging environments or when running multiple workers).
    # With threading mode, multiple Gunicorn workers are fine but each will
    # run its own scheduler — use SCHEDULER_ENABLED=false on extra workers.
    SCHEDULER_ENABLED = os.environ.get('SCHEDULER_ENABLED', 'true').lower() == 'true'
    


# ============================================================================
# Application Factory
# ============================================================================

def create_app(config_class=Config):
    """Create and configure the Flask application"""
    app = Flask(__name__)

    # ========================================================================
    # Trust Railway's (and any other) reverse proxy
    # ========================================================================
    # Railway/Render/Heroku terminate HTTPS at their edge proxy and forward
    # plain HTTP to this app internally. Without this, Flask thinks every
    # request is HTTP, which makes url_for(..., _external=True) generate
    # http:// URLs even on an https:// page -> browsers block them as
    # "mixed content". ProxyFix reads the X-Forwarded-Proto/Host/For headers
    # the proxy sets, so Flask sees the real scheme/host.
    # x_proto=1 / x_host=1 / x_for=1 = trust these headers from 1 proxy hop.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    app.config.from_object(config_class)
    
    # Initialize extensions
    db.init_app(app)
    mail.init_app(app)
    migrate.init_app(app, db)
    
    # ========================================================================
    # WebSocket Initialization (CRITICAL - must be in correct order)
    # ========================================================================
    # Step 1: Initialize base message WebSocket (creates socketio instance).
    #         Uses async_mode='threading' + simple-websocket (Python 3.13 safe).
    #         Install dep: pip install simple-websocket
    socketio = init_message_websocket(app)
    
    # Step 2: Initialize thread WebSocket handlers using the same socketio instance.
    #         This MUST happen BEFORE the server starts.
    thread_ws_manager.init_socketio(app, socketio)
    
    # ========================================================================
    # Logging Configuration
    # ========================================================================
    if not app.debug and not app.testing:
        # Create logs directory if itesn't exist
        if not os.path.exists('logs'):
            os.mkdir('logs')
        
        # File handler for error logs
        file_handler = RotatingFileHandler(
            'logs/studyhub.log',
            maxBytes=10240000,  # 10MB
            backupCount=10
        )
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
        ))
        file_handler.setLevel(logging.INFO)
        app.logger.addHandler(file_handler)
        
        app.logger.setLevel(logging.INFO)
        app.logger.info('StudyHub startup')
    
    # ========================================================================
    # Error Handlers
    # ========================================================================
    
    @app.errorhandler(400)
    def bad_request(error):
        app.logger.error(f"400 Bad Request: {error}")
        return jsonify({
            "status": "error",
            "message": "Bad request - Invalid data format"
        }), 400
    
    @app.errorhandler(404)
    def not_found(error):
        return jsonify({
            "status": "error",
            "message": "Resource not found"
        }), 404
    
    @app.errorhandler(500)
    def internal_error(error):
        app.logger.error(f"500 Internal Error: {error}")
        db.session.rollback()
        return jsonify({
            "status": "error",
            "message": "Internal server error"
        }), 500
    
    # ========================================================================
    # Security Headers
    # ========================================================================
    
    @app.after_request
    def set_security_headers(response):
        """Add security headers to all responses.

        FIX: Guard against WebSocket upgrade requests. When async_mode='threading'
        is used, the after_request hook fires on WebSocket connections too.
        Trying to set headers on an already-upgraded connection causes:
            AssertionError: write() before start_response
        """
        # Skip header injection for WebSocket upgrade connections
        if request.environ.get('wsgi.websocket'):
            return response

        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-XSS-Protection'] = '1; mode=block'
        
        # Only set HSTS in production with HTTPS
        if not app.debug and app.config.get('SESSION_COOKIE_SECURE'):
            response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
        
        return response
    
    # ========================================================================
    # Request Logging
    # ========================================================================
    
    @app.before_request
    def log_request():
        """Log important requests without exposing sensitive data"""
        if request.method in ['POST', 'PUT', 'DELETE']:
            app.logger.info(f"{request.method} {request.path} from {request.remote_addr}")
    
    # ========================================================================
    # Register Blueprints
    # ========================================================================
    app.register_blueprint(waitlist_bp)
    app.register_blueprint(google_bp, url_prefix='/google')
    app.register_blueprint(student_bp)
    
    # ========================================================================
    # Routes
    # ========================================================================
    
    @app.route("/")
    def home():
        """Landing page"""
        return render_template("index.html")
    
    @app.route("/health")
    def health_check():
        """Health check endpoint for monitoring"""
        try:
            # Check database connection
            db.session.execute('SELECT 1')
            
            # Check email configuration
            email_status = bool(
                app.config.get('MAIL_USERNAME') and app.config.get('MAIL_PASSWORD')
            )

            # ── Scheduler status ───────────────────────────────────────────────
            from scheduler import scheduler
            scheduler_jobs = []
            if scheduler.running:
                for job in scheduler.get_jobs():
                    scheduler_jobs.append({
                        "id":            job.id,
                        "name":          job.name,
                        "next_run_time": (
                            job.next_run_time.isoformat()
                            if job.next_run_time else None
                        ),
                    })

            return jsonify({
                "status":           "healthy",
                "database":         "connected",
                "email_configured": email_status,
                "mail_server":      app.config.get('MAIL_SERVER'),
                "scheduler": {
                    "running": scheduler.running,
                    "jobs":    scheduler_jobs,
                },
            }), 200

        except Exception as e:
            app.logger.error(f"Health check failed: {e}")
            return jsonify({
                "status":   "unhealthy",
                "database": "disconnected",
            }), 500
    
    @app.route("/robots.txt")
    def robots():
        """Robots.txt for search engines"""
        return """User-agent: *
Allow: /
Disallow: /admin/
Disallow: /api/
Disallow: /student/profile/
""", 200, {'Content-Type': 'text/plain'}
    
    # ========================================================================
    # Shell Context
    # ========================================================================
    
    @app.shell_context_processor
    def make_shell_context():
        """Add database and models to Flask shell"""
        from models import User, WaitlistSignup, Post, Comment
        return {
            'db':             db,
            'User':           User,
            'WaitlistSignup': WaitlistSignup,
            'Post':           Post,
            'Comment':        Comment,
        }

    # ========================================================================
    # Scheduler Initialization
    # ========================================================================
    # Placed LAST in create_app() so the scheduler only starts after all
    # extensions, blueprints, and DB models are fully registered.
    #
    # Guard: SCHEDULER_ENABLED env var (default: true).
    # Set SCHEDULER_ENABLED=false in staging or multi-worker setups.
    # ========================================================================
    if app.config.get('SCHEDULER_ENABLED', True):
        from scheduler import init_scheduler
        init_scheduler(app)
    else:
        app.logger.info("[Scheduler] Disabled via SCHEDULER_ENABLED=false")

    return app, socketio  # Return both app and socketio for proper initialization


# ============================================================================
# Application Instance
# ============================================================================

# Create app and socketio using the factory
app, socketio = create_app()


# ============================================================================
# Run Application
# ============================================================================

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5001))
    host = "0.0.0.0"

    # ── Scheduler status line for startup banner ───────────────────────────────
    from scheduler import scheduler as _sched
    sched_status = "✅ Running" if _sched.running else "❌ Not started"
    sched_jobs   = len(_sched.get_jobs()) if _sched.running else 0
    next_runs    = ""
    if _sched.running:
        lines = [
            f"    • {j.name}: {j.next_run_time.strftime('%a %Y-%m-%d %H:%M UTC')}"
            for j in _sched.get_jobs()
            if j.next_run_time
        ]
        next_runs = "\n" + "\n".join(lines)

    print("\n" + "="*60)
    print("🚀 StudyHub Starting...")
    print("="*60)
    print(f"📧 Email:            {os.environ.get('MAIL_USERNAME', 'Not configured')}")
    print(f"🗄️  Database:         {os.environ.get('DATABASE_NEW_URL', 'Not configured')}")
    print(f"🔑 Secret Key:       {'✅ Set' if os.environ.get('SECRET_KEY') else '❌ Missing'}")
    print(f"🌐 WebSocket:        threading + simple-websocket (Python 3.13 compatible)")
    print(f"💬 Thread WebSocket: {'✅ Initialized' if thread_ws_manager.socketio else '❌ Not initialized'}")
    print(f"⏰ Scheduler:        {sched_status} ({sched_jobs} job(s)){next_runs}")
    print("="*60)
    print(f"🔗 Server running on: http://{host}:{port}")
    print(f"🔗 Local access:      http://127.0.0.1:{port}")
    print(f"🔗 Network access:    http://localhost:{port}")
    print("="*60 + "\n")
    
    # Create database tables if they don't exist
    with app.app_context():
        db.create_all()
        print("✅ Database tables created/verified\n")
    
    # Run with SocketIO (socketio already has all handlers registered)
    # NOTE: use_reloader=False is required — prevents scheduler double-start
    #       and avoids the threading WebSocket handler being registered twice.
    socketio.run(
        app,
        debug=True,
        host=host,
        port=port,
        use_reloader=False,
    )


# ============================================================================
# Production Entry Point (for Gunicorn)
# ============================================================================
# In production (Gunicorn), the 'app' and 'socketio' variables are already
# created at module level. The WebSocket handlers are already registered
# because create_app() ran when the module loaded.
#
# Run with threading mode (no special worker class needed):
#   gunicorn -w 1 app:app
#
# To disable the scheduler in a specific dyno/container (e.g. a web replica):
#   SCHEDULER_ENABLED=false gunicorn -w 1 app:app
#
# ⚠️  Keep -w 1 if using APScheduler to avoid duplicate scheduled jobs.
#     If you need multiple workers, set SCHEDULER_ENABLED=false on all
#     but one worker, or switch to a distributed scheduler (e.g. Celery Beat).
