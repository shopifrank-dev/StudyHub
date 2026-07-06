"""
Learnora AI Chat System - PRODUCTION VERSION
Features: Account rotation, multiple providers, full frontend support
Removed: SmartChatCache (all caching logic)
Added: AI title generation, reset-title endpoint, Cloudinary attachment upload,
       conversation delete, manual title update, paginated message fetch

PROVIDERS (in fallback order — highest free TPM first):
  1. Cerebras   (~60K TPM free)  — gpt-oss-120b (production) + dynamic discovery
  2. Groq       (~30K TPM free)  — llama-4-scout (vision!) + llama-3.3-70b + dynamic discovery
  3. Mistral    (500K TPM free*) — mistral-small-latest / mistral-medium-latest (provider aliases)
  4. OpenRouter (pay-per-use)    — meta-llama/llama-4-scout (vision!) + static model list

MODEL SELECTION STRATEGY:
  - Cerebras & Groq: dynamic model discovery via GET /v1/models at startup (background thread),
    ranked by MODEL_PRIORITY. Falls back to static lists gracefully.
  - Mistral: uses provider-managed alias IDs that Mistral keeps pointing to their
    current best model — zero maintenance required.
  - OpenRouter: uses static model list (no dynamic discovery); OpenAI-compatible
    endpoint at https://openrouter.ai/api/v1. Supports vision via llama-4-scout.

ENV VARS:
  CEREBRAS_API_KEY_1   … CEREBRAS_API_KEY_5
  GROQ_API_KEY_1       … GROQ_API_KEY_10
  MISTRAL_API_KEY_1    … MISTRAL_API_KEY_5
  OPENROUTER_API_KEY_1 … OPENROUTER_API_KEY_5
  (single-key fallback: CEREBRAS_API_KEY, GROQ_API_KEY, MISTRAL_API_KEY, OPENROUTER_API_KEY)

CHANGES (2026-06):
  - Cerebras: primary model updated to gpt-oss-120b (the only current production model
    on Cerebras public endpoints); llama models retained as fallbacks.
  - Groq: added meta-llama/llama-4-scout-17b-16e-instruct as primary;
    it's the latest Groq production model AND supports vision (multimodal).
    llama-3.3-70b-versatile + llama-3.1-8b-instant remain as reliable fallbacks.
  - Mistral: replaced mistral-small-2506 (legacy/deprecated) and ministral-8b-2410 (old)
    with provider-managed aliases mistral-small-latest and mistral-medium-latest,
    which Mistral automatically keeps pointing to the current recommended version.
  - All providers: background dynamic model discovery added — models are refreshed
    from /v1/models at startup so new models are picked up without code changes.
  - Vision: Groq's llama-4-scout is now flagged as vision-capable, enabling
    image processing for Groq providers.
  - OpenRouter: added as final fallback provider with OpenAI-compatible API;
    supports vision via meta-llama/llama-4-scout; uses OPENROUTER_API_KEY_1…_5.
  - Provider-type blacklisting: MultiProviderManager now tracks failures per
    provider type (cerebras/groq/mistral/openrouter). After
    PROVIDER_FAILURE_THRESHOLD (3) failures within PROVIDER_FAILURE_WINDOW (5 min),
    ALL keys for that provider type are blacklisted for PROVIDER_BLACKLIST_DURATION
    (30 min). This handles load/outage events like Cerebras being fully down —
    the system stops hammering it and routes all traffic to the next provider
    without waiting for individual key cooldowns to expire.
  - Response cleaning: clean_ai_response() strips reasoning/scratchpad blocks
    (<think>…</think> etc.), stray SSE protocol artefacts, bare code-fence
    wrapping, and excessive blank lines before responses are stored or returned.
    Applied to streamed chat replies, sync thread replies, and title generation."""

import io
import os
import re
import json
import base64
import requests
import mimetypes
import pandas as pd
from werkzeug.utils import secure_filename
from routes.student.helpers import (
    token_required, success_response, error_response
)
from PIL import Image

from flask import request, render_template,  jsonify, Response, stream_with_context, current_app, Blueprint
from extensions import db
from models import AIConversation, AIUsageQuota, Post, User
import datetime
import logging
import threading

learnora_bp = Blueprint('learnora', __name__, url_prefix='/learnora')


# Setup logging
# NOTE: without a basicConfig call, the root logger defaults to WARNING level,
# which silently drops every logger.info(...) call below (you'd only ever see
# .warning()/.error() lines in the terminal). If your app's entrypoint (app.py)
# already calls logging.basicConfig(...) elsewhere, this is redundant but
# harmless (basicConfig is a no-op if handlers already exist).
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)


# ===========================================================
# MODEL PRIORITY LISTS
#
# These serve two purposes:
#   1. Static fallback when dynamic discovery is unavailable.
#   2. Ranking template for discovered models (known models appear first
#      in priority order; unknown new models are appended as extras).
#
# Mistral exception: uses provider-managed alias IDs that are always
# current — no dynamic discovery needed, no manual updates required.
# ===========================================================

# Groq vision models — these support image input (multimodal).
GROQ_VISION_MODELS = {
    "meta-llama/llama-4-scout-17b-16e-instruct",
}

CEREBRAS_MODELS = [
    "gpt-oss-120b"      # current production model on Cerebras (June 2026)
  
]

GROQ_MODELS = [
    "openai/gpt-oss-120b",                    # Best reasoning & complex tasks
    "meta-llama/llama-4-scout-17b-16e-instruct", # Vision + multimodal + strong general chat
    "qwen/qwen3-32b",                         # Fast, smart, cost-efficient middle tier
]

MISTRAL_MODELS = [
    "mistral-large-2512",     # Best reasoning & quality
    "mistral-medium-latest",  # Balanced quality/cost
    "ministral-3b-2512",      # Ultra-fast & cheap fallback
]

# OpenRouter vision models — these support image input (multimodal).
OPENROUTER_VISION_MODELS = {
    "meta-llama/llama-4-scout",
}
OPENROUTER_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free", # Default
    "google/gemma-4-31b-it:free",            # Vision tasks
    "nousresearch/hermes-3-405b:free",       # Escalation
]



# Non-chat model filter: skip these during dynamic model discovery.
NON_CHAT_PATTERN = re.compile(
    r"whisper|embed|guard|tts|moderation|transcribe|ocr|safeguard|vision-only",
    re.IGNORECASE,
)

# Provider order — mirrors multiProvider.js PROVIDER_ORDER
PROVIDER_ORDER = ["cerebras", "groq", "mistral", "openrouter"]


# ===========================================================
# MULTI-PROVIDER API KEY MANAGER
# ===========================================================

class MultiProviderManager:
    """Manage multiple API providers and rotate between them"""

    # How many failures within the sliding window triggers a provider-level blacklist
    PROVIDER_FAILURE_THRESHOLD = 3
    # Sliding window for counting failures (seconds)
    PROVIDER_FAILURE_WINDOW = 300   # 5 minutes
    # How long a blacklisted provider stays locked out (seconds)
    PROVIDER_BLACKLIST_DURATION = 1800  # 30 minutes

    def __init__(self):
        self.providers = self._load_providers()
        self.current_provider_index = 0

        # Per-key cooldown  {provider_name: datetime}  — unchanged
        self.failed_providers: dict = {}
        self.cooldown_period = 3600   # 1-hour per-key cooldown

        # Per-provider-type failure tracking
        # {provider_type: [datetime, ...]}  — rolling list of failure timestamps
        self._provider_type_failures: dict = {}
        # {provider_type: datetime}  — when the type was blacklisted
        self._blacklisted_types: dict = {}

        # Kick off background model discovery after providers are loaded
        self._warm_model_discovery()

    # ----------------------------------------------------------
    # Provider loading
    # ----------------------------------------------------------

    def _load_providers(self):
        """Load Cerebras, Groq, Mistral, and OpenRouter providers from environment variables.
        Each provider supports multiple keys (e.g. CEREBRAS_API_KEY_1 … _5).
        Falls back to the no-suffix var (CEREBRAS_API_KEY) when only one key exists.
        Provider order mirrors multiProvider.js: cerebras → groq → mistral → openrouter.
        """
        providers = []

        PROVIDER_DEFS = [
            {
                "id":         "cerebras",
                "type":       "cerebras",
                "base_url":   "https://api.cerebras.ai/v1",
                "env_prefix": "CEREBRAS_API_KEY",
                "max_keys":   10,
                "models":     CEREBRAS_MODELS,
                # Cerebras currently has no vision-capable models on public endpoints.
                "vision_models": set(),
            },
            {
                "id":         "groq",
                "type":       "groq",
                "base_url":   "https://api.groq.com/openai/v1",
                "env_prefix": "GROQ_API_KEY",
                "max_keys":   10,
                "models":     GROQ_MODELS,
                # llama-4-scout supports multimodal image input on Groq.
                "vision_models": GROQ_VISION_MODELS,
            },
            {
                "id":         "mistral",
                "type":       "mistral",
                "base_url":   "https://api.mistral.ai/v1",
                "env_prefix": "MISTRAL_API_KEY",
                "max_keys":   5,
                "models":     MISTRAL_MODELS,
                "vision_models": set(),
            },
            {
                "id":         "openrouter",
                "type":       "openrouter",
                "base_url":   "https://openrouter.ai/api/v1",
                "env_prefix": "OPENROUTER_API_KEY",
                "max_keys":   5,
                "models":     OPENROUTER_MODELS,
                # llama-4-scout supports multimodal image input on OpenRouter.
                "vision_models": OPENROUTER_VISION_MODELS,
            },
        ]

        for defn in PROVIDER_DEFS:
            keys = []
            for i in range(1, defn["max_keys"] + 1):
                key = os.getenv(f"{defn['env_prefix']}_{i}")
                if key and key.strip():
                    keys.append((i, key.strip()))

            # Single-key fallback (no suffix)
            if not keys:
                key = os.getenv(defn["env_prefix"])
                if key and key.strip():
                    keys.append((0, key.strip()))

            if not keys:
                continue

            logger.info(f"🔧 {defn['id']}: {len(keys)} key(s) loaded")

            primary_model   = defn["models"][0]
            vision_models   = defn.get("vision_models", set())
            # Primary vision model: first model in models list that supports vision,
            # or None if none of the listed models support vision.
            primary_vision  = next((m for m in defn["models"] if m in vision_models), None)
            supports_vision = primary_vision is not None

            for key_index, api_key in keys:
                providers.append({
                    "name":                   f"{defn['id']}_{key_index}",
                    "api_key":                api_key,
                    "base_url":               defn["base_url"],
                    "text_model":             primary_model,
                    "vision_model":           primary_vision,
                    "supports_vision":        supports_vision,
                    "type":                   defn["type"],
                    "text_model_fallbacks":   defn["models"],
                    "vision_model_fallbacks": [m for m in defn["models"] if m in vision_models],
                    # Store reference to provider definition for dynamic updates
                    "_provider_id":           defn["id"],
                    "_vision_models":         vision_models,
                })

        logger.info(f"🔧 Loaded {len(providers)} provider slot(s) across {PROVIDER_ORDER}")
        return providers

    # ----------------------------------------------------------
    # Dynamic model discovery (background)
    # ----------------------------------------------------------

    def _warm_model_discovery(self):
        """
        Spawn a background thread to discover available models from each
        provider's /v1/models endpoint. Updates each provider slot's model
        lists in-place once discovery completes.

        Mistral is skipped — it uses provider-managed aliases that are
        already self-updating.
        """
        # Group provider slots by provider_id to avoid redundant API calls
        seen: dict = {}
        for p in self.providers:
            pid = p.get("_provider_id")
            if pid and pid not in seen:
                seen[pid] = p

        def _discover():
            for pid, provider_slot in seen.items():
                if pid in ("mistral", "openrouter"):
                    continue  # mistral aliases are self-updating; openrouter uses static list
                self._fetch_and_apply_models(pid, provider_slot)

        t = threading.Thread(target=_discover, daemon=True)
        t.start()

    def _fetch_and_apply_models(self, provider_id: str, representative_slot: dict):
        """
        Fetch /v1/models for a provider and update all matching provider slots
        with the ranked model list.
        """
        import requests as _req

        base_url   = representative_slot["base_url"]
        api_key    = representative_slot["api_key"]
        priority   = CEREBRAS_MODELS if provider_id == "cerebras" else GROQ_MODELS

        try:
            resp = _req.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=8,
            )
            resp.raise_for_status()
            data   = resp.json()
            all_ids = [m.get("id", "") for m in data.get("data", []) if m.get("id")]

            # Rank: known-priority models first (in order), then unknown chat models.
            known   = [m for m in priority if m in all_ids]
            unknown = [m for m in all_ids if m not in priority and not NON_CHAT_PATTERN.search(m)]
            ranked  = known + sorted(unknown)

            if not ranked:
                logger.warning(f"⚠️  {provider_id}: discovery returned 0 usable models — keeping static list")
                return

            logger.info(f"✅ {provider_id}: discovered {len(ranked)} chat model(s) — updating provider slots")

        except Exception as e:
            logger.warning(f"⚠️  {provider_id}: model discovery failed ({e}) — keeping static list")
            return

        # Determine vision-capable models in the discovered set
        vision_models = representative_slot.get("_vision_models", set())
        primary_model  = ranked[0]
        primary_vision = next((m for m in ranked if m in vision_models), None)

        # Apply to every slot belonging to this provider
        for p in self.providers:
            if p.get("_provider_id") == provider_id:
                p["text_model"]            = primary_model
                p["text_model_fallbacks"]  = ranked
                p["vision_model"]          = primary_vision
                p["supports_vision"]       = primary_vision is not None
                p["vision_model_fallbacks"] = [m for m in ranked if m in vision_models]

    # ----------------------------------------------------------
    # Runtime helpers
    # ----------------------------------------------------------

    # ----------------------------------------------------------
    # Provider-type blacklist helpers
    # ----------------------------------------------------------

    def _record_provider_type_failure(self, provider_type: str):
        """
        Record a failure for the given provider type and blacklist the entire
        type if it has exceeded PROVIDER_FAILURE_THRESHOLD failures within
        PROVIDER_FAILURE_WINDOW seconds.
        """
        now = datetime.datetime.utcnow()
        window_start = now - datetime.timedelta(seconds=self.PROVIDER_FAILURE_WINDOW)

        # Prune old failures outside the sliding window
        timestamps = self._provider_type_failures.get(provider_type, [])
        timestamps = [t for t in timestamps if t >= window_start]
        timestamps.append(now)
        self._provider_type_failures[provider_type] = timestamps

        failure_count = len(timestamps)
        logger.info(
            f"📊 Provider type '{provider_type}' failure count in last "
            f"{self.PROVIDER_FAILURE_WINDOW}s: {failure_count}/{self.PROVIDER_FAILURE_THRESHOLD}"
        )

        if failure_count >= self.PROVIDER_FAILURE_THRESHOLD:
            if provider_type not in self._blacklisted_types:
                logger.error(
                    f"🚫 Provider type '{provider_type}' has failed {failure_count} times "
                    f"— blacklisting ALL {provider_type} keys for "
                    f"{self.PROVIDER_BLACKLIST_DURATION // 60} min"
                )
            self._blacklisted_types[provider_type] = now

    def _is_provider_type_blacklisted(self, provider_type: str) -> bool:
        """Return True if the provider type is currently blacklisted."""
        blacklisted_at = self._blacklisted_types.get(provider_type)
        if not blacklisted_at:
            return False
        elapsed = (datetime.datetime.utcnow() - blacklisted_at).total_seconds()
        if elapsed >= self.PROVIDER_BLACKLIST_DURATION:
            # Blacklist expired — clear it and reset failure counters
            del self._blacklisted_types[provider_type]
            self._provider_type_failures.pop(provider_type, None)
            logger.info(f"✅ Provider type '{provider_type}' blacklist expired — re-enabling")
            return False
        return True

    # ----------------------------------------------------------
    # Runtime helpers
    # ----------------------------------------------------------

    def get_working_provider(self, needs_vision=False):
        """Get next working provider, skipping blacklisted provider types and failed keys."""
        if not self.providers:
            logger.error("❌ No API providers configured!")
            return None

        # Clear expired per-key cooldowns
        now = datetime.datetime.utcnow()
        self.failed_providers = {
            name: fail_time for name, fail_time in self.failed_providers.items()
            if (now - fail_time).total_seconds() < self.cooldown_period
        }

        attempts = 0
        while attempts < len(self.providers):
            provider = self.providers[self.current_provider_index]
            provider_type = provider.get("_provider_id", provider["name"])

            # Skip entire provider type if blacklisted
            if self._is_provider_type_blacklisted(provider_type):
                logger.info(f"⏭️ Skipping {provider['name']} — provider type '{provider_type}' is blacklisted")
                self.rotate()
                attempts += 1
                continue

            # Skip individual failed key
            if provider["name"] in self.failed_providers:
                self.rotate()
                attempts += 1
                continue

            if needs_vision and not provider["supports_vision"]:
                logger.info(f"⏭️ Skipping {provider['name']} (no vision support)")
                self.rotate()
                attempts += 1
                continue

            logger.info(f"✅ Using provider: {provider['name']}")
            return provider

        logger.error("❌ All providers are in cooldown or blacklisted!")
        return None

    def mark_provider_failed(self, provider_name: str, error_message: str = ""):
        """
        Mark a specific provider key as failed (per-key cooldown) AND record
        a failure against its provider type so repeated failures trigger a
        full type-level blacklist.
        """
        self.failed_providers[provider_name] = datetime.datetime.utcnow()
        logger.warning(f"⚠️ Provider key '{provider_name}' failed: {error_message}")

        # Find the provider type for this key and record the type-level failure
        for p in self.providers:
            if p["name"] == provider_name:
                provider_type = p.get("_provider_id", provider_name)
                self._record_provider_type_failure(provider_type)
                break

    def rotate(self):
        """Move to next provider"""
        self.current_provider_index = (self.current_provider_index + 1) % len(self.providers)

    def get_stats(self):
        """Get provider statistics"""
        provider_details = []
        for p in self.providers:
            provider_type = p.get("_provider_id", p["name"])
            provider_details.append({
                "name": p["name"],
                "provider_type": provider_type,
                "text_model": p.get("text_model"),
                "supports_vision": p.get("supports_vision", False),
                "vision_model": p.get("vision_model"),
                "available_models": len(p.get("text_model_fallbacks", [])),
                "key_failed": p["name"] in self.failed_providers,
                "type_blacklisted": self._is_provider_type_blacklisted(provider_type),
                "type_failure_count": len(self._provider_type_failures.get(provider_type, [])),
            })

        blacklisted_info = {}
        for pt, ts in self._blacklisted_types.items():
            elapsed = (datetime.datetime.utcnow() - ts).total_seconds()
            remaining = max(0, self.PROVIDER_BLACKLIST_DURATION - elapsed)
            blacklisted_info[pt] = {
                "blacklisted_at": ts.isoformat(),
                "remaining_seconds": int(remaining),
            }

        return {
            "total_providers": len(self.providers),
            "active_providers": sum(
                1 for p in self.providers
                if p["name"] not in self.failed_providers
                and not self._is_provider_type_blacklisted(p.get("_provider_id", p["name"]))
            ),
            "failed_keys": list(self.failed_providers.keys()),
            "blacklisted_provider_types": blacklisted_info,
            "current_provider": self.providers[self.current_provider_index]["name"] if self.providers else None,
            "providers": provider_details,
        }

# Initialize provider manager
provider_manager = MultiProviderManager()


# ===========================================================
# FILE HANDLER
# ===========================================================

class FileHandler:
    def __init__(self):
        self.total_files = 0
        self.doc_files = 0
        self.code_files = 0
        self.image_files = 0
        self.total_tokens = 0
        self.extracted_texts = []
        self.has_images = False

    def process_files(self, files):
        """Process all uploaded files and extract text/data"""
        logger.info(f"📁 Processing {len(files)} files")

        for file_key in files:
            file = files[file_key]
            filename = file.filename.lower()

            logger.info(f"📄 Processing file: {filename}")

            ftype = self.detect_type(filename)

            try:
                if ftype == "code":
                    text = self.extract_code(file)
                    self.code_files += 1

                elif ftype == "document":
                    text = self.extract_document(file, filename)
                    self.doc_files += 1

                elif ftype == "image":
                    text = self.extract_image_base64(file)
                    self.image_files += 1
                    self.has_images = True

                else:
                    text = f"[Unsupported file type: {filename}]"
                    logger.warning(f"⚠️ Unsupported file type: {filename}")

                token_count = self.estimate_tokens(text)
                self.total_tokens += token_count

                if text and not text.startswith("[ERROR"):
                    self.extracted_texts.append({
                        "type": ftype,
                        "content": text,
                        "filename": file.filename
                    })
                    logger.info(f"✅ Extracted {len(text)} chars from {filename}")
                else:
                    logger.error(f"❌ Failed to extract from {filename}: {text}")

                self.total_files += 1

            except Exception as e:
                logger.error(f"❌ Error processing {filename}: {str(e)}", exc_info=True)
                continue

        logger.info(f"✅ Processed {self.total_files} files: {self.doc_files} docs, {self.code_files} code, {self.image_files} images")

        return {
            "texts": self.extracted_texts,
            "tokens": self.total_tokens,
            "has_images": self.has_images,
            "info": {
                "total_files": self.total_files,
                "document_files": self.doc_files,
                "code_files": self.code_files,
                "image_files": self.image_files,
            }
        }

    def detect_type(self, filename):
        if filename.endswith((".py", ".js", ".java", ".ts", ".cpp", ".html", ".css", ".php", ".rb", ".c", ".h")):
            return "code"
        if filename.endswith((".pdf", ".doc", ".docx", ".txt", ".csv")):
            return "document"
        if filename.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
            return "image"
        return "unknown"

    def extract_code(self, file):
        """Extract code from file"""
        try:
            file.seek(0)
            content = file.read().decode("utf-8", errors="ignore")
            if len(content) > 400_000:
                return "[ERROR: Code file too large. Max 400KB]"
            return content
        except Exception as e:
            logger.error(f"Code extraction error: {str(e)}")
            return f"[ERROR reading code: {str(e)}]"

    def extract_document(self, file, filename):
        """Extract text from documents"""
        try:
            file.seek(0)

            if filename.endswith(".txt"):
                content = file.read().decode("utf-8", errors="ignore")
                if len(content) > 400_000:
                    return "[ERROR: Text file too large. Max 400KB]"
                return content

            if filename.endswith(".csv"):
                df = pd.read_csv(file)
                content = df.to_string()
                if len(content) > 400_000:
                    return "[ERROR: CSV too large. Max 400KB]"
                return content

            if filename.endswith(".doc"):
                return "[ERROR: .doc files not supported. Please upload .docx]"

            if filename.endswith(".docx"):
                import docx2txt
                import tempfile
                file.seek(0)
                with tempfile.NamedTemporaryFile(delete=False, suffix='.docx') as tmp:
                    tmp.write(file.read())
                    tmp_path = tmp.name
                content = docx2txt.process(tmp_path)
                os.unlink(tmp_path)
                if len(content) > 400_000:
                    return "[ERROR: Document too large. Max 400KB]"
                return content

            if filename.endswith(".pdf"):
                text = ""
                try:
                    import PyPDF2
                    file.seek(0)
                    pdf_reader = PyPDF2.PdfReader(file)
                    for page in pdf_reader.pages:
                        text += page.extract_text() or ""
                    logger.info("✅ Extracted PDF using PyPDF2")
                except ImportError:
                    pass
                except Exception as e:
                    logger.warning(f"PyPDF2 failed: {str(e)}")

                if len(text) > 400_000:
                    return "[ERROR: PDF too large. Max 400KB]"
                return text

            return "[ERROR: Unsupported document format]"
        except Exception as e:
            logger.error(f"Document extraction error: {str(e)}")
            return f"[ERROR reading document: {str(e)}]"

    def extract_image_base64(self, file):
        """
        Convert image to base64 data URI for vision models.
        Returns a data:mime;base64,... string used later in build_messages()
        only when the active provider supports vision.
        """
        try:
            file.seek(0)

            # Check file size (5MB max)
            file.seek(0, 2)
            size = file.tell()
            file.seek(0)

            if size > 5_000_000:
                return "[ERROR: Image too large. Max 5MB]"

            # Get image dimensions
            img = Image.open(file)
            width, height = img.size

            # Estimate token cost
            baseline_pixels = 512 * 512
            baseline_tokens = 1610
            actual_pixels = width * height
            estimated_tokens = int((actual_pixels / baseline_pixels) * baseline_tokens)

            if estimated_tokens > 60_000:
                return f"[ERROR: Image resolution too high ({width}x{height}). Please resize.]"

            # Reset file pointer and encode to base64
            file.seek(0)
            image_data = base64.b64encode(file.read()).decode('utf-8')

            mime_type = mimetypes.guess_type(file.filename)[0] or 'image/jpeg'

            # Return as data URI — used directly as the image_url value
            return f"data:{mime_type};base64,{image_data}"

        except Exception as e:
            logger.error(f"Image processing error: {str(e)}")
            return f"[ERROR processing image: {str(e)}]"

    def estimate_tokens(self, text):
        """Estimate token count (4 chars ≈ 1 token)"""
        if not text or text.startswith("[ERROR"):
            return 0
        return len(text) // 4


# ===========================================================
# HELPER FUNCTIONS
# ===========================================================

def upload_user_files(files, user_id):
    """
    Upload user files to Cloudinary and return metadata.
    Uses cloudinary_storage.upload_file directly to avoid the buggy
    upload_ai_file wrapper in storage.py.
    """
    from storage import cloudinary_storage, FilenameService

    uploaded_files = []

    logger.info(f"📤 Uploading {len(files)} files for user {user_id}")

    for file_key in files:
        file = files[file_key]

        if not file or not file.filename:
            continue

        safe_filename = secure_filename(file.filename)
        fname_lower = safe_filename.lower()

        # Determine category and Cloudinary resource type
        if fname_lower.endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')):
            file_type = 'image'
            resource_type = 'image'
        elif fname_lower.endswith(('.py', '.js', '.java', '.ts', '.cpp', '.txt', '.html', '.css')):
            file_type = 'code'
            resource_type = 'raw'
        elif fname_lower.endswith(('.pdf', '.doc', '.docx', '.csv')):
            file_type = 'document'
            resource_type = 'raw'
        else:
            file_type = 'unknown'
            resource_type = 'auto'

        # Generate a unique filename via FilenameService
        _, _, generated_filename = FilenameService.get_ai_temp_path(user_id, safe_filename)
        folder_path = f"ai-uploads/user_{user_id}"

        # Read size before upload
        file.seek(0, 2)
        file_size = file.tell()
        file.seek(0)

        # Upload to Cloudinary
        result = cloudinary_storage.upload_file(file, folder_path, generated_filename, resource_type)

        if result["success"]:
            mime_type = mimetypes.guess_type(safe_filename)[0] or 'application/octet-stream'
            uploaded_files.append({
                "filename": file.filename,
                "url": result["url"],
                "public_id": result.get("public_id"),
                "size": file_size,
                "mime_type": mime_type,
                "type": file_type
            })
            logger.info(f"✅ Uploaded {file.filename} to Cloudinary")
        else:
            logger.error(f"❌ Failed to upload {file.filename}: {result['error']}")

    return uploaded_files


def generate_conversation_title(first_message, provider=None):
    """
    Generate a short, descriptive conversation title using AI.
    Falls back to message truncation if AI call fails or no provider available.
    """
    if provider:
        try:
            headers = {"Content-Type": "application/json"}

            if provider.get("api_key"):
                headers["Authorization"] = f"Bearer {provider['api_key']}"

            endpoint = f"{provider['base_url']}/chat/completions"
            payload = {
                "model": provider["text_model"],
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Generate a concise, descriptive title (3–6 words) for a study conversation "
                            "based on the user's first message. "
                            "Return ONLY the title text — no quotes, no punctuation, no explanation."
                        )
                    },
                    {
                        "role": "user",
                        "content": first_message[:500]
                    }
                ],
                "max_tokens": 60,
                "stream": False
            }

            response = requests.post(endpoint, headers=headers, json=payload, timeout=10)

            if response.status_code == 200:
                result = response.json()
                try:
                    message = result["choices"][0]["message"]
                    # Reasoning models (e.g. gpt-oss-120b) can return the visible
                    # answer under "content", with internal reasoning tokens under
                    # "reasoning_content". If max_tokens is too low, "content" can
                    # come back missing entirely while reasoning_content is set —
                    # that's what was causing the bare 'content' KeyError.
                    title = (message.get("content") or message.get("reasoning_content") or "")
                    title = clean_ai_response(title).strip('"\'')
                except (KeyError, IndexError, TypeError):
                    logger.warning(f"⚠️ Unexpected title response shape: {result}")
                    title = ""

                if title and len(title) <= 100:
                    logger.info(f"✅ AI-generated title: '{title}'")
                    return title
            else:
                logger.warning(f"⚠️ Title generation got HTTP {response.status_code}: {response.text[:300]}")

        except Exception as e:
            logger.warning(f"⚠️ AI title generation failed, using fallback: {str(e)}")

    # Fallback: truncate the first message cleanly
    clean = ' '.join(first_message.split())
    return clean if len(clean) <= 60 else clean[:57] + '...'


# ===========================================================
# RESPONSE CLEANING
# ===========================================================

# Patterns that some reasoning/chat models emit at the start of responses
# even when not asked to — strip these before showing text to the user.
_REASONING_PREFIX_RE = re.compile(
    r"^(<think>.*?</think>|<reasoning>.*?</reasoning>|<scratchpad>.*?</scratchpad>)\s*",
    re.DOTALL | re.IGNORECASE,
)

# Stray SSE/data protocol artefacts that can leak into streamed content
_SSE_ARTIFACT_RE = re.compile(r"^data:\s*", re.MULTILINE)

# Some models wrap the whole reply in triple back-ticks with no language tag
_BARE_CODE_FENCE_RE = re.compile(r"^```\s*\n(.*?)\n```\s*$", re.DOTALL)


def clean_ai_response(text: str) -> str:
    """
    Sanitise a raw AI response before it is stored or sent to the client.

    Cleaning steps (in order):
      1. Strip leading/trailing whitespace.
      2. Remove internal reasoning/scratchpad blocks that some models emit
         (e.g. <think>…</think> from DeepSeek-style models).
      3. Remove stray SSE protocol prefixes ("data: ") that can bleed through
         when a streamed chunk is accidentally captured verbatim.
      4. Unwrap a response that is *entirely* a bare triple-back-tick block
         with no language tag (the model mistakenly wrapping prose in fences).
      5. Collapse three-or-more consecutive blank lines to two (keeps intentional
         whitespace but removes runaway vertical padding).
      6. Final strip.

    The function is intentionally conservative — it does NOT strip markdown
    formatting (bold, headers, code blocks with language tags) because those
    are meaningful to the frontend renderer.
    """
    if not text:
        return ""

    # 1. Initial strip
    text = text.strip()

    # 2. Remove leading reasoning/scratchpad blocks
    text = _REASONING_PREFIX_RE.sub("", text).strip()

    # 3. Remove stray SSE prefixes
    text = _SSE_ARTIFACT_RE.sub("", text).strip()

    # 4. Unwrap bare code fences wrapping the entire response (prose mistake)
    bare_match = _BARE_CODE_FENCE_RE.match(text)
    if bare_match:
        inner = bare_match.group(1).strip()
        # Only unwrap if the inner text looks like prose (no newline-separated
        # code lines that start with keywords), to avoid stripping real code.
        first_line = inner.splitlines()[0] if inner else ""
        looks_like_code = re.match(
            r"^\s*(def |class |import |from |#include|function |var |const |let |public |private )",
            first_line,
        )
        if not looks_like_code:
            text = inner

    # 5. Collapse excessive blank lines (3+ → 2)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 6. Final strip
    return text.strip()


# ===========================================================
# STUDY ASSISTANT - MULTI-PROVIDER VERSION
# ===========================================================

class StudyAssistant:
    def __init__(self, provider, conversation_messages=None):
        self.provider = provider
        self.conversation_history = conversation_messages or []
        self.model = None           # set by select_model()
        self.base_system = (
            "You are Learnora, an intelligent study assistant. "
            "Provide clear, accurate, and helpful responses."
        )

    def should_summarize(self):
        return len(self.conversation_history) > 10

    @staticmethod
    def _preview_text(content) -> str:
        """Best-effort short text preview of a message's content, whether it's
        a plain string or a list of content parts (file/image attachments)."""
        if isinstance(content, str):
            return content[:100]
        if isinstance(content, list):
            text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            return " ".join(text_parts)[:100] if text_parts else "[attachment]"
        return str(content)[:100]

    def summarize_conversation(self):
        if len(self.conversation_history) <= 10:
            return self.conversation_history

        old_messages = self.conversation_history[:-5]
        recent_messages = self.conversation_history[-5:]

        summary_text = "Previous conversation summary:\n"
        for msg in old_messages:
            preview = self._preview_text(msg.get("content"))
            if msg["role"] == "user":
                summary_text += f"- User asked: {preview}...\n"
            elif msg["role"] == "assistant":
                summary_text += f"- Assistant answered: {preview}...\n"

        summarized = [{"role": "system", "content": summary_text}]
        summarized.extend(recent_messages)
        return summarized

    def get_working_messages(self):
        """
        Return the conversation history slice to send to the provider,
        sanitized down to {role, content} only.

        IMPORTANT: messages stored in conversation.messages (the DB column)
        carry extra bookkeeping fields — timestamp, attachments, is_continue,
        model, provider, is_complete, error — for our own app's use. Several
        providers (Cerebras in particular) run strict schema validation on
        the chat completions body and will reject the *entire request* with
        a 400 if a message object contains any property outside role/content.
        This is why the first message in a conversation (empty history) works
        fine, but every message after it fails — the moment there's stored
        history to replay, those extra keys go along for the ride.
        """
        raw = self.summarize_conversation() if self.should_summarize() else self.conversation_history[-10:]
        return [
            {"role": m.get("role", "user"), "content": m.get("content", "")}
            for m in raw
            if m.get("role") and m.get("content") is not None
        ]

    def select_model(self, has_images: bool):
        """
        Pick the best model for this request.

        Vision flow:
          1. If images present AND provider supports vision → use vision_model
          2. If images present but provider has no vision support → fall back to
             text_model and set a flag so build_messages() strips image parts
        """
        if has_images and self.provider.get("supports_vision"):
            self.model = self.provider["vision_model"]
            self.vision_active = True
            logger.info(f"🤖 Vision model selected: {self.model}")
        else:
            self.model = self.provider["text_model"]
            self.vision_active = False
            if has_images:
                logger.warning(
                    f"⚠️ Provider {self.provider['name']} does not support vision — "
                    "images will be described as text-only placeholders."
                )
            else:
                logger.info(f"🤖 Text model selected: {self.model}")

    def build_messages(self, user_input, extracted_data, mode, post_content=None):
        """
        Build the full message array for the API call.

        Vision handling:
          - If self.vision_active is True: images are embedded as base64 data URIs
            inside image_url content parts (standard OpenAI vision format).
          - If self.vision_active is False: image items are replaced with a plain
            text notice so non-vision models don't crash or silently ignore them.
        """
        messages = []

        messages.append({"role": "system", "content": self.base_system})
        messages.append({"role": "system", "content": self.get_mode_prompt(mode)})

        context_messages = self.get_working_messages()
        messages.extend(context_messages)

        user_content_parts = []

        if user_input:
            user_content_parts.append({
                "type": "text",
                "text": f"**Question:** {user_input}"
            })

        if post_content:
            user_content_parts.append({
                "type": "text",
                "text": (
                    f"\n\n**Referenced Post:**\n"
                    f"Title: {post_content['title']}\n\n"
                    f"Content: {post_content['content']}"
                )
            })

        logger.info(f"📎 Building message with {len(extracted_data)} file(s), vision_active={getattr(self, 'vision_active', False)}")

        for item in extracted_data:
            if item["type"] == "image":
                if getattr(self, "vision_active", False):
                    # Send as base64 data URI — primary vision path
                    user_content_parts.append({
                        "type": "image_url",
                        "image_url": {"url": item["content"]}
                    })
                    logger.info(f"🖼️ Added base64 image: {item['filename']}")
                else:
                    # Non-vision model fallback — send a text placeholder
                    user_content_parts.append({
                        "type": "text",
                        "text": (
                            f"\n\n**[Attached Image: {item['filename']}]**\n"
                            "_(Image content cannot be displayed — the current model does not support vision. "
                            "Please describe what you need help with regarding this image.)_"
                        )
                    })
                    logger.info(f"📝 Image replaced with text placeholder: {item['filename']}")
            else:
                content_preview = item["content"][:5000]
                if len(item["content"]) > 5000:
                    content_preview += "\n\n[... content truncated ...]"

                user_content_parts.append({
                    "type": "text",
                    "text": (
                        f"\n\n**Attached {item['type'].upper()} File:** `{item['filename']}`\n"
                        f"```\n{content_preview}\n```"
                    )
                })
                logger.info(f"📄 Added file: {item['filename']}")

        # Only use the OpenAI-style array-of-parts content format when we're
        # actually embedding a real image (vision). Several providers —
        # Mistral in particular ("Extra inputs are not permitted" is a known
        # Mistral validation error) and apparently Groq's non-vision models
        # too — validate the message schema strictly and reject the
        # multimodal array format unless a vision model is actually in use.
        # The old rule ("array only if more than 1 part") broke the moment a
        # file or referenced post was attached, since that always produces
        # 2+ parts. Collapsing everything to a single string is the safest,
        # most portable choice whenever there's no image to embed.
        has_image_part = any(p["type"] == "image_url" for p in user_content_parts)

        if has_image_part:
            messages.append({"role": "user", "content": user_content_parts})
        else:
            combined_text = "\n".join(p["text"] for p in user_content_parts if p["type"] == "text")
            messages.append({"role": "user", "content": combined_text})

        logger.info(f"✅ Built message with {len(messages)} parts")
        return messages

    def get_mode_prompt(self, mode):
        mode_prompts = {
            "deep_think": (
                "Provide extremely thorough explanations. Break down complex concepts "
                "into simple steps. Use examples and analogies."
            ),
            "fast_response": (
                "Provide concise, direct answers. Be brief but accurate."
            ),
            "programming": (
                "You are an expert programming tutor. Review code carefully, explain logic, "
                "identify bugs, suggest improvements, and provide working examples."
            ),
            "research": (
                "Act as a research assistant. Provide well-researched information."
            ),
            "summarize": (
                "Summarize the provided content concisely. Extract key points."
            ),
            "explain": (
                "Explain concepts as if teaching a student. Use simple language."
            )
        }
        return mode_prompts.get(mode, "Respond helpfully and clearly.")

    def _is_model_error(self, error_msg: str) -> bool:
        """
        Return True if the error looks like a missing/invalid model,
        meaning we should retry with the next fallback model rather than
        marking the whole provider as failed.
        """
        lower = error_msg.lower()
        model_error_signals = [
            "model not found",
            "no endpoints found",
            "invalid model",
            "model does not exist",
            "unknown model",
            "model_not_found",
            "404",
        ]
        return any(sig in lower for sig in model_error_signals)

    def advance_to_fallback_model(self, has_images: bool) -> bool:
        """
        Try the next model in the provider's fallback chain.
        Returns True if a new model was selected, False if the chain is exhausted.
        """
        key = "vision_model_fallbacks" if has_images and getattr(self, "vision_active", False) \
              else "text_model_fallbacks"
        fallbacks: list = self.provider.get(key, [])

        current = self.model
        try:
            idx = fallbacks.index(current)
            next_models = fallbacks[idx + 1:]
        except ValueError:
            next_models = fallbacks  # current model wasn't in list, try all

        for next_model in next_models:
            self.model = next_model
            logger.info(f"🔄 Model fallback: {current} → {next_model}")
            return True

        logger.warning(f"⚠️ Model fallback chain exhausted for {self.provider['name']}")
        return False

    def stream_response(self, messages, has_images: bool = False):
        """
        Stream AI response with error handling.
        Detects model-not-found errors and automatically retries with the
        next model in the provider's fallback chain.

        Sets self._provider_exhausted = True when all models in this provider
        have been tried and failed, so generate() knows to switch providers.
        """
        MAX_MODEL_RETRIES = max(len(CEREBRAS_MODELS), len(GROQ_MODELS), len(MISTRAL_MODELS), len(OPENROUTER_MODELS))
        self._provider_exhausted = False

        for model_attempt in range(MAX_MODEL_RETRIES):
            yield from self._do_stream(messages)
            # _do_stream sets self._model_error_occurred if a model error occurred
            if not getattr(self, "_model_error_occurred", False):
                return  # clean exit

            # Try next fallback model
            advanced = self.advance_to_fallback_model(has_images)
            if not advanced:
                # All models in this provider exhausted — signal caller to switch providers
                logger.warning(f"⚠️ All models exhausted for provider {self.provider['name']} — needs provider switch")
                self._provider_exhausted = True
                return

            self._model_error_occurred = False
            logger.info(f"🔁 Retrying stream with model: {self.model}")
            yield f"data: {json.dumps({'type': 'model_retry', 'new_model': self.model})}\n\n"

    def _do_stream(self, messages):
        """
        Internal: perform one streaming request and yield SSE chunks.
        Sets self._model_error_occurred = True if a model-not-found error
        is detected so stream_response() knows to retry.
        """
        self._model_error_occurred = False

        headers = {"Content-Type": "application/json"}

        if self.provider["api_key"]:
            headers["Authorization"] = f"Bearer {self.provider['api_key']}"

        data = {
            "model": self.model,
            "messages": messages,
            "stream": True,
        }

        logger.info(f"🚀 Streaming — provider: {self.provider['name']}, model: {self.model}")
        endpoint_url = f"{self.provider['base_url']}/chat/completions"

        try:
            response = requests.post(
                endpoint_url,
                headers=headers,
                json=data,
                stream=True,
                timeout=60
            )

            # Catch model-not-found at the HTTP level (some providers return 404)
            if response.status_code == 404:
                error_body = response.text[:200]
                logger.warning(f"⚠️ 404 for model {self.model}: {error_body}")
                self._model_error_occurred = True
                return

            response.raise_for_status()

            response_complete = False

            for line in response.iter_lines():
                if line:
                    line = line.decode('utf-8')

                    if line.startswith(':'):
                        continue

                    if line.startswith('data: '):
                        line = line[6:]

                    if line == '[DONE]':
                        response_complete = True
                        yield "data: [DONE]\n\n"
                        break

                    try:
                        chunk = json.loads(line)

                        if 'error' in chunk:
                            error_msg = chunk['error'].get('message', str(chunk['error']))
                            logger.error(f"❌ API error in stream: {error_msg}")

                            # Model-not-found error → trigger model fallback
                            if self._is_model_error(error_msg):
                                logger.warning(f"⚠️ Model error detected: {error_msg}")
                                self._model_error_occurred = True
                                return

                            if 'rate limit' in error_msg.lower() or 'quota' in error_msg.lower():
                                yield f"data: {json.dumps({'rate_limit': True, 'error': error_msg})}\n\n"
                            else:
                                yield f"data: {json.dumps({'error': error_msg})}\n\n"
                            break

                        content = chunk.get('choices', [{}])[0].get('delta', {}).get('content', '')

                        if content:
                            yield f"data: {json.dumps({'content': content})}\n\n"

                        finish_reason = chunk.get('choices', [{}])[0].get('finish_reason')

                        if finish_reason == 'length':
                            yield f"data: {json.dumps({'incomplete': True, 'reason': 'token_limit'})}\n\n"
                            response_complete = False
                            break
                        elif finish_reason == 'stop':
                            response_complete = True
                        elif finish_reason == 'content_filter':
                            yield f"data: {json.dumps({'error': 'Response filtered'})}\n\n"
                            break

                    except json.JSONDecodeError:
                        continue

            yield f"data: {json.dumps({'complete': response_complete})}\n\n"
            logger.info(f"✅ Stream complete: {response_complete}")

        except requests.exceptions.Timeout:
            logger.error("⏱️ Request timeout")
            yield f"data: {json.dumps({'error': 'Request timed out', 'timeout': True})}\n\n"
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            body = e.response.text[:500] if e.response is not None else ""
            logger.error(f"❌ HTTP error {status} from {self.provider['name']}: {body}")
            if status == 404:
                self._model_error_occurred = True
                return
            yield f"data: {json.dumps({'error': f'HTTP {status}', 'http_error': True})}\n\n"
        except Exception as e:
            logger.error(f"❌ Stream error: {str(e)}", exc_info=True)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"


def _call_provider_sync(
    messages: list,
    provider: dict,
    type: str = "",
    max_tokens: int | None = None,
) -> str | None:
    """
    Non-streaming provider call for use in background threads.

    Used exclusively by the Thread WebSocket system when Learnora is
    triggered inside a group chat.
    """
    import requests as req_lib
    import json as _json

    # Some call types need more headroom than a normal chat reply.
    # Meeting notes summarize a whole conversation into structured JSON,
    # so they need a much higher ceiling.
    DEFAULT_MAX_TOKENS = {
        "meeting_notes": 2000,
    }
    if max_tokens is None:
        max_tokens = DEFAULT_MAX_TOKENS.get(type, 500)

    headers = {"Content-Type": "application/json"}
    if provider["api_key"]:
        headers["Authorization"] = f"Bearer {provider['api_key']}"

    payload = {
        "model":      provider["text_model"],
        "messages":   messages,
        "stream":     False,
        "max_tokens": max_tokens
    }

    try:
        response = req_lib.post(
            f"{provider['base_url']}/chat/completions",
            headers=headers,
            json=payload,
            timeout=30
        )
        response.raise_for_status()
        data = response.json()

        message = data["choices"][0]["message"]

        # Reasoning models (DeepSeek-reasoner, QwQ, etc.) sometimes return
        # the usable text in "reasoning_content" rather than "content",
        # or leave "content" empty. Prefer content when it's non-empty,
        # fall back to reasoning_content otherwise.
        content = (message.get("content") or "").strip()
        reasoning_content = (message.get("reasoning_content") or "").strip()
        raw = content or reasoning_content

        if not raw:
            logger.warning(
                f"Learnora sync call ({type or 'default'}): empty content "
                f"and reasoning_content from provider {provider.get('name')}"
            )
            return None

        return clean_ai_response(raw)

    except req_lib.exceptions.Timeout:
        logger.error(f"Learnora sync call ({type or 'default'}): timeout")
        provider_manager.mark_provider_failed(provider["name"], "timeout")
        return None

    except req_lib.exceptions.HTTPError as e:
        logger.error(f"Learnora sync call ({type or 'default'}) HTTP error: {e}")
        provider_manager.mark_provider_failed(provider["name"], str(e))
        return None

    except Exception as e:
        logger.error(f"Learnora sync call ({type or 'default'}) error: {e}", exc_info=True)
        provider_manager.mark_provider_failed(provider["name"], str(e))
        return None

# ===========================================================
# FLASK ROUTES
# ===========================================================


# -----------------------------------------------------------
# MAIN CHAT
# -----------------------------------------------------------
@learnora_bp.route("/", methods=["GET"])
@token_required
def learnora_page(current_user):
    return render_template('learnora/learnora.html')


@learnora_bp.route("/api/chat", methods=["POST"])
@token_required
def chat(current_user):
    """
    Main chat endpoint with multi-provider streaming support.

    Form fields:
        conversation_id (int, required): Target conversation
        message (str, required): User's message
        mode (str, optional): Response mode — fast_response | deep_think | programming | research | summarize | explain
        post_id (int, optional): Related post ID for context
        is_continue (str, optional): "true" if continuing an incomplete response
        files (multipart, optional): Attachments (images, docs, code)
    """
    try:
        # ── Resolve conversation ─────────────────────────────
        conversation_id = request.form.get("conversation_id")

        if not conversation_id:
            return jsonify({"error": "conversation_id is required"}), 400

        conversation = AIConversation.query.filter_by(
            id=conversation_id,
            user_id=current_user.id
        ).first()

        if not conversation:
            return jsonify({"error": "Conversation not found"}), 404

        if conversation.is_archived:
            return jsonify({"error": "This conversation has been archived"}), 410

        # ── Hard message cap ─────────────────────────────────
        if conversation.total_messages >= 500:
            return jsonify({"error": "Message limit reached (500 messages per conversation)"}), 429

        # ── Daily quota ──────────────────────────────────────
        quota = AIUsageQuota.query.filter_by(user_id=current_user.id).first()
        if not quota:
            quota = AIUsageQuota(user_id=current_user.id, daily_messages_limit=50)
            db.session.add(quota)
            db.session.commit()

        today = datetime.date.today()
        if quota.last_reset_date != today:
            quota.daily_messages_used = 0
            quota.last_reset_date = today
            db.session.commit()

        if quota.daily_messages_used >= quota.daily_messages_limit:
            return jsonify({
                "error": f"Daily limit reached ({quota.daily_messages_limit} messages). Try again tomorrow."
            }), 429

        # ── Parse request fields ─────────────────────────────
        user_message = request.form.get("message", "").strip()
        mode = request.form.get("mode", "fast_response")
        post_id = request.form.get("post_id")
        is_continue = request.form.get("is_continue", "false").lower() == "true"

        # If no message text was sent, this may be a request to generate the
        # AI's first reply to a message that was already seeded when the
        # conversation was created (POST /api/conversation/new with
        # initial_message). In that case, reuse the last stored user turn
        # instead of requiring the student to retype it.
        seeded_reuse = False
        if not user_message:
            existing_messages = conversation.messages or []
            if existing_messages and existing_messages[-1].get("role") == "user":
                last_content = existing_messages[-1].get("content", "")
                if isinstance(last_content, list):
                    last_content = " ".join(
                        p.get("text", "") for p in last_content if isinstance(p, dict)
                    )
                user_message = (last_content or "").strip()
                seeded_reuse = bool(user_message)

        if not user_message:
            return jsonify({"error": "Message cannot be empty"}), 400

        logger.info(
            f"💬 Chat request: user={current_user.id}, conv={conversation_id}, "
            f"mode={mode}, is_continue={is_continue}, "
            f"provider_stats={provider_manager.get_stats()}"
        )

        # ── Start Cloudinary upload in background ────────────
        files = request.files
        _upload_result = {}

        def _do_cloudinary_upload():
            _upload_result["data"] = upload_user_files(files, current_user.id)

        upload_thread = threading.Thread(target=_do_cloudinary_upload, daemon=True)
        upload_thread.start()

        # ── Process files for AI context (local, no network) ─
        handler = FileHandler()
        file_result = handler.process_files(files)
        logger.info(f"📊 File processing: {file_result['info']}")

        # ── Optional post context ────────────────────────────
        post_content = None
        if post_id:
            post = Post.query.get(post_id)
            if post:
                post_content = {
                    "title": post.title,
                    "content": post.text_content or ""
                }

        # ── Pick a working provider ──────────────────────────
        provider = provider_manager.get_working_provider(needs_vision=file_result["has_images"])

        if not provider:
            return jsonify({
                "error": "All AI providers are currently unavailable. Please try again later.",
                "stats": provider_manager.get_stats()
            }), 503

        # ── Build assistant + messages ────────────────────────
        # When reusing a seeded message, exclude it from history — it's the
        # question being asked right now, not prior context, so it should
        # only appear once in the built message list (not duplicated).
        if seeded_reuse:
            conversation_messages_copy = list(conversation.messages[:-1]) if conversation.messages else []
        else:
            conversation_messages_copy = list(conversation.messages) if conversation.messages else []

        assistant = StudyAssistant(provider, conversation_messages_copy)
        assistant.select_model(file_result["has_images"])

        messages = assistant.build_messages(
            user_message,
            file_result["texts"],
            mode,
            post_content
        )

        # ── Join upload thread, then persist user message ─────
        upload_thread.join(timeout=30)
        uploaded_file_metadata = _upload_result.get("data", [])

        db.session.refresh(conversation)

        if seeded_reuse:
            # Already stored at creation time — just bump timing, don't
            # duplicate it in conversation.messages.
            conversation.last_message_at = datetime.datetime.utcnow()
        else:
            user_msg = {
                "role": "user",
                "content": user_message,
                "timestamp": datetime.datetime.utcnow().isoformat(),
                "attachments": uploaded_file_metadata,
                "is_continue": is_continue
            }
            conversation.messages.append(user_msg)
            conversation.total_messages += 1
            conversation.last_message_at = datetime.datetime.utcnow()

        is_first_message = (conversation.total_messages == 1)

        quota.daily_messages_used += 1
        quota.last_message_time = datetime.datetime.utcnow()

        db.session.commit()
        db.session.expunge(conversation)
        db.session.expunge(quota)

        # ── Generate title in background (first message only) ─
        # Skip if this was a seeded message — create_conversation() already
        # kicked off title generation for it, no need to do it twice.
        if is_first_message and not seeded_reuse:
            _app       = current_app._get_current_object()
            _conv_id   = int(conversation_id)
            _first_msg = user_message
            _provider  = provider

            def _do_title():
                try:
                    title = generate_conversation_title(_first_msg, _provider)
                    with _app.app_context():
                        conv = AIConversation.query.get(_conv_id)
                        if conv:
                            conv.title = title
                            db.session.commit()
                            logger.info(f"✅ Background title saved for conv {_conv_id}: '{title}'")
                except Exception as _e:
                    logger.warning(f"⚠️ Background title generation failed: {_e}")

            threading.Thread(target=_do_title, daemon=True).start()

        # ── Streaming response with provider rotation ─────────
        def generate():
            nonlocal provider
            full_response = ""
            response_complete = True
            error_occurred = False
            error_message = None
            retries = 0
            max_retries = 3

            yield f"data: {json.dumps({'type': 'start', 'model': assistant.model, 'provider': provider['name']})}\n\n"

            while retries < max_retries:
                error_in_stream = False

                for chunk in assistant.stream_response(messages, has_images=file_result["has_images"]):
                    yield chunk

                    if chunk.startswith("data: "):
                        try:
                            data = json.loads(chunk[6:])

                            if 'content' in data:
                                full_response += data['content']
                            elif 'incomplete' in data:
                                response_complete = False
                            elif 'complete' in data:
                                response_complete = data['complete']
                            elif data.get('type') == 'model_retry':
                                # Model fallback already handled inside stream_response
                                pass
                            elif 'error' in data:
                                error_message = data['error']
                                error_occurred = True

                                if data.get('rate_limit') or data.get('timeout') or data.get('http_error'):
                                    error_in_stream = True
                                    provider_manager.mark_provider_failed(provider['name'], error_message)
                                    provider_manager.rotate()

                                    next_provider = provider_manager.get_working_provider(
                                        needs_vision=file_result["has_images"]
                                    )

                                    if next_provider and retries < max_retries - 1:
                                        logger.info(f"🔄 Switching provider to {next_provider['name']}...")
                                        provider = next_provider
                                        assistant.provider = next_provider
                                        assistant.select_model(file_result["has_images"])

                                        yield f"data: {json.dumps({'type': 'provider_switch', 'new_provider': provider['name']})}\n\n"

                                        retries += 1
                                        error_occurred = False
                                        error_message = None
                                        break
                                    else:
                                        response_complete = False
                                        break
                                else:
                                    response_complete = False
                                    break
                        except Exception:
                            pass

                if not error_in_stream:
                    # Also check if stream_response exhausted all models in the provider
                    # (model-not-found failures that never produced a stream error chunk)
                    if getattr(assistant, '_provider_exhausted', False) and not full_response:
                        logger.warning(f"⚠️ Provider {provider['name']} exhausted all models — forcing provider switch")
                        provider_manager.mark_provider_failed(provider['name'], "all models returned 404")
                        provider_manager.rotate()

                        next_provider = provider_manager.get_working_provider(
                            needs_vision=file_result["has_images"]
                        )

                        if next_provider and retries < max_retries - 1:
                            logger.info(f"🔄 Switching provider to {next_provider['name']} after model exhaustion...")
                            provider = next_provider
                            assistant.provider = next_provider
                            assistant.select_model(file_result["has_images"])
                            assistant._provider_exhausted = False

                            yield f"data: {json.dumps({'type': 'provider_switch', 'new_provider': provider['name']})}\n\n"

                            retries += 1
                            error_occurred = False
                            error_message = None
                        else:
                            response_complete = False
                            break
                    else:
                        break

            # ── Persist assistant response ────────────────────
            try:
                with db.session.begin_nested():
                    conv = db.session.query(AIConversation).get(conversation_id)

                    cleaned_response = clean_ai_response(full_response) if full_response else ""

                    assistant_msg = {
                        "role": "assistant",
                        "content": cleaned_response if cleaned_response else "[Error: No response]",
                        "model": assistant.model,
                        "provider": provider['name'],
                        "timestamp": datetime.datetime.utcnow().isoformat(),
                        "is_complete": response_complete,
                        "error": error_message
                    }

                    conv.messages.append(assistant_msg)
                    conv.total_messages += 1
                    conv.tokens_used += handler.total_tokens + len(cleaned_response) // 4
                    conv.is_last_message_complete = response_complete

                    if not response_complete:
                        conv.last_incomplete_message = cleaned_response

                    if error_occurred:
                        conv.error_count += 1

                    db.session.commit()

            except Exception as e:
                logger.error(f"❌ Error saving assistant response: {str(e)}", exc_info=True)

            done_payload = {
                'type': 'done',
                'tokens': handler.total_tokens,
                'complete': response_complete,
                'can_continue': not response_complete and not error_occurred,
                'provider': provider['name']
            }
            yield f"data: {json.dumps(done_payload)}\n\n"
       

        return Response(
            stream_with_context(generate()),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no'
            }
        )

    except Exception as e:
        logger.error(f"❌ Chat error: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# -----------------------------------------------------------
# CONVERSATION MANAGEMENT
# -----------------------------------------------------------

@learnora_bp.route("/api/conversation/new", methods=["POST"])
@token_required
def create_conversation(current_user):
    """
    Create a new AI conversation session.

    JSON body:
        initial_message (str, optional): The topic/question the student typed
            before the conversation existed. It is stored immediately as the
            conversation's first user message (so the AI has it as context
            right away) and used to seed a title (instant truncated fallback,
            upgraded to an AI-generated one in the background).

            No AI reply is generated yet at this point — this endpoint is a
            plain JSON response, not a stream. To actually get the AI's
            response to it, call POST /api/chat with this same
            conversation_id and an EMPTY "message" field; the endpoint will
            detect the unanswered seeded message and respond to it instead
            of requiring the text to be retyped.

    Returns:
        conversation_id to be used in subsequent /api/chat calls.
    """
    try:
        data = request.get_json(silent=True) or {}
        initial_message = (data.get("initial_message") or "").strip()[:2000]

        conversation = AIConversation(
            user_id=current_user.id,
            messages=[],
            total_messages=0,
            tokens_used=0
        )
        db.session.add(conversation)
        db.session.commit()

        if initial_message:
            # Seed it as a real first message, exactly like /api/chat would
            # store it, minus attachments (none exist at creation time).
            seed_msg = {
                "role": "user",
                "content": initial_message,
                "timestamp": datetime.datetime.utcnow().isoformat(),
                "attachments": [],
                "is_continue": False
            }
            conversation.messages.append(seed_msg)
            conversation.total_messages = 1
            conversation.last_message_at = datetime.datetime.utcnow()

            # Instant, synchronous fallback title — so the UI has something
            # sensible immediately instead of "New Conversation".
            clean = ' '.join(initial_message.split())
            conversation.title = clean if len(clean) <= 60 else clean[:57] + '...'
            db.session.commit()

            # Upgrade to an AI-generated title in the background, same
            # pattern used for the first message in /api/chat.
            _app     = current_app._get_current_object()
            _conv_id = conversation.id
            _msg     = initial_message

            def _do_initial_title():
                try:
                    provider = provider_manager.get_working_provider(needs_vision=False)
                    title = generate_conversation_title(_msg, provider)
                    with _app.app_context():
                        conv = AIConversation.query.get(_conv_id)
                        if conv:
                            conv.title = title
                            db.session.commit()
                            logger.info(f"✅ Initial title generated for conv {_conv_id}: '{title}'")
                except Exception as e:
                    logger.warning(f"⚠️ Initial title generation failed: {e}")

            threading.Thread(target=_do_initial_title, daemon=True).start()

        return jsonify({
            "status": "success",
            "data": {
                "conversation_id": conversation.id,
                "title": conversation.title,
                "initial_message": initial_message or None,
                "created_at": conversation.created_at.isoformat()
            }
        }), 201

    except Exception as e:
        logger.error(f"Error creating conversation: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@learnora_bp.route("/api/conversation/list", methods=["GET"])
@token_required
def get_conversations(current_user):
    """
    Fetch the user's active (non-archived) conversation list.

    Query params:
        limit (int, optional): Max results, default 50, max 100
    """
    try:
        limit = min(request.args.get("limit", 50, type=int), 100)

        conversations = AIConversation.query.filter_by(
            user_id=current_user.id,
            is_archived=False
        ).order_by(AIConversation.last_message_at.desc()).limit(limit).all()

        conversation_data = [
            {
                "conversation_id": conv.id,
                "title": conv.title,
                "total_messages": conv.total_messages,
                "tokens_used": conv.tokens_used,
                "is_last_message_complete": conv.is_last_message_complete,
                "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None,
                "created_at": conv.created_at.isoformat() if conv.created_at else None
            }
            for conv in conversations
        ]

        return jsonify({
            "status": "success",
            "data": conversation_data,
            "count": len(conversation_data)
        })

    except Exception as e:
        logger.error(f"Error loading conversations: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@learnora_bp.route("/api/conversations/<int:conversation_id>", methods=["GET"])
@token_required
def get_conversation(current_user, conversation_id):
    """
    Fetch a conversation with paginated messages.

    Query params:
        page     (int, optional): Page number, default 1. Paginates from most recent.
        per_page (int, optional): Messages per page, default 20, max 100.
    """
    try:
        conversation = AIConversation.query.filter_by(
            id=conversation_id,
            user_id=current_user.id
        ).first()

        if not conversation:
            return jsonify({"status": "error", "message": "Conversation not found"}), 404

        page = request.args.get("page", 1, type=int)
        per_page = min(request.args.get("per_page", 20, type=int), 100)

        all_messages = conversation.messages or []
        total_count = len(all_messages)

        end_idx = max(0, total_count - (page - 1) * per_page)
        start_idx = max(0, end_idx - per_page)
        paginated_messages = all_messages[start_idx:end_idx]

        return jsonify({
            "status": "success",
            "data": {
                "id": conversation.id,
                "title": conversation.title,
                "messages": paginated_messages,
                "pagination": {
                    "page": page,
                    "per_page": per_page,
                    "total_messages": total_count,
                    "has_more": start_idx > 0
                },
                "total_messages": conversation.total_messages,
                "tokens_used": conversation.tokens_used,
                "is_last_message_complete": conversation.is_last_message_complete,
                "last_message_at": conversation.last_message_at.isoformat() if conversation.last_message_at else None,
                "created_at": conversation.created_at.isoformat()
            }
        })

    except Exception as e:
        logger.error(f"Error loading conversation: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@learnora_bp.route("/api/conversation/<int:conversation_id>", methods=["DELETE"])
@token_required
def delete_conversation(current_user, conversation_id):
    """
    Archive (soft-delete) a conversation.
    Archived conversations no longer appear in the list and cannot receive new messages.
    """
    try:
        conversation = AIConversation.query.filter_by(
            id=conversation_id,
            user_id=current_user.id
        ).first()

        if not conversation:
            return jsonify({"status": "error", "message": "Conversation not found"}), 404

        if conversation.is_archived:
            return jsonify({"status": "error", "message": "Conversation is already archived"}), 409

        conversation.is_archived = True
        db.session.commit()

        logger.info(f"🗂️ Conversation {conversation_id} archived by user {current_user.id}")

        return jsonify({
            "status": "success",
            "message": "Conversation archived successfully"
        })

    except Exception as e:
        logger.error(f"❌ Error archiving conversation {conversation_id}: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@learnora_bp.route("/api/conversation/<int:conversation_id>/clear", methods=["POST"])
@token_required
def clear_conversation(current_user, conversation_id):
    """
    Clear all messages from a conversation while keeping the conversation record.
    """
    try:
        conversation = AIConversation.query.filter_by(
            id=conversation_id,
            user_id=current_user.id
        ).first()

        if not conversation:
            return jsonify({"status": "error", "message": "Conversation not found"}), 404

        conversation.messages = []
        conversation.total_messages = 0
        conversation.tokens_used = 0
        conversation.title = "New Conversation"
        conversation.last_message_at = None
        conversation.last_incomplete_message = None
        conversation.is_last_message_complete = True
        conversation.error_count = 0
        db.session.commit()

        logger.info(f"🧹 Conversation {conversation_id} cleared by user {current_user.id}")

        return jsonify({
            "status": "success",
            "message": "Conversation history cleared"
        })

    except Exception as e:
        logger.error(f"❌ Error clearing conversation {conversation_id}: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


# -----------------------------------------------------------
# TITLE MANAGEMENT
# -----------------------------------------------------------

@learnora_bp.route("/api/conversation/<int:conversation_id>/title", methods=["PUT"])
@token_required
def update_conversation_title(current_user, conversation_id):
    """
    Manually override the conversation title.

    JSON body:
        title (str, required): New title, max 200 characters
    """
    try:
        data = request.get_json() or {}
        new_title = data.get("title", "").strip()

        if not new_title:
            return jsonify({"status": "error", "message": "title field is required"}), 400

        if len(new_title) > 200:
            return jsonify({"status": "error", "message": "Title too long (max 200 characters)"}), 400

        conversation = AIConversation.query.filter_by(
            id=conversation_id,
            user_id=current_user.id
        ).first()

        if not conversation:
            return jsonify({"status": "error", "message": "Conversation not found"}), 404

        conversation.title = new_title
        db.session.commit()

        return jsonify({
            "status": "success",
            "data": {
                "conversation_id": conversation_id,
                "title": new_title
            }
        })

    except Exception as e:
        logger.error(f"❌ Error updating title for conversation {conversation_id}: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@learnora_bp.route("/api/chat/reset-title", methods=["POST"])
@token_required
def reset_conversation_title(current_user):
    """
    Regenerate the AI-generated title for a conversation.
    Finds the first user message and re-runs the title generation prompt.

    JSON body:
        conversation_id (int, required): Target conversation
    """
    try:
        data = request.get_json() or {}
        conversation_id = data.get("conversation_id")

        if not conversation_id:
            return jsonify({"status": "error", "message": "conversation_id is required"}), 400

        conversation = AIConversation.query.filter_by(
            id=conversation_id,
            user_id=current_user.id
        ).first()

        if not conversation:
            return jsonify({"status": "error", "message": "Conversation not found"}), 404

        if not conversation.messages:
            return jsonify({"status": "error", "message": "Conversation has no messages to generate a title from"}), 400

        first_user_message = next(
            (msg.get("content", "") for msg in conversation.messages if msg.get("role") == "user"),
            None
        )

        if not first_user_message:
            return jsonify({"status": "error", "message": "No user messages found in conversation"}), 400

        provider = provider_manager.get_working_provider(needs_vision=False)

        new_title = generate_conversation_title(first_user_message, provider)

        conversation.title = new_title
        db.session.commit()

        logger.info(f"🔄 Title reset for conversation {conversation_id}: '{new_title}'")

        return jsonify({
            "status": "success",
            "data": {
                "conversation_id": conversation_id,
                "title": new_title
            }
        })

    except Exception as e:
        logger.error(f"❌ Error resetting title: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


# -----------------------------------------------------------
# FILE UPLOADS
# -----------------------------------------------------------

@learnora_bp.route("/api/upload/attachment", methods=["POST"])
@token_required
def upload_post_attachment(current_user):
    """
    Upload a post attachment (image or document) to Cloudinary.
    Intended for use before creating a post — returns the URL to embed.

    Form fields:
        file (multipart, required): The file to upload

    Allowed types:
        Images   — jpg, jpeg, png, webp, gif  (max 10MB)
        Documents — pdf, doc, docx, txt, csv  (max 10MB)

    Returns:
        url, public_id, filename, size, mime_type, file_category
    """
    try:
        from storage import cloudinary_storage, FilenameService

        if 'file' not in request.files:
            return jsonify({"status": "error", "message": "No file provided"}), 400

        file = request.files['file']

        if not file or not file.filename:
            return jsonify({"status": "error", "message": "File has no name"}), 400

        safe_filename = secure_filename(file.filename)
        fname_lower = safe_filename.lower()

        ALLOWED = {
            'image':    ('.jpg', '.jpeg', '.png', '.webp', '.gif'),
            'document': ('.pdf', '.doc', '.docx', '.txt', '.csv'),
        }

        file_category = None
        resource_type = None
        for category, exts in ALLOWED.items():
            if any(fname_lower.endswith(ext) for ext in exts):
                file_category = category
                resource_type = 'image' if category == 'image' else 'raw'
                break

        if not file_category:
            allowed_list = ", ".join(ext for exts in ALLOWED.values() for ext in exts)
            return jsonify({
                "status": "error",
                "message": f"Unsupported file type. Allowed: {allowed_list}"
            }), 415

        file.seek(0, 2)
        file_size = file.tell()
        file.seek(0)

        MAX_SIZE = 10 * 1024 * 1024
        if file_size > MAX_SIZE:
            return jsonify({
                "status": "error",
                "message": f"File too large ({file_size // 1024}KB). Maximum is 10MB."
            }), 413

        if file_size == 0:
            return jsonify({"status": "error", "message": "File is empty"}), 400

        folder, generated_filename = FilenameService.get_post_file_path(
            current_user.id,
            safe_filename,
            file_category
        )

        result = cloudinary_storage.upload_file(file, folder, generated_filename, resource_type)

        if not result["success"]:
            logger.error(f"❌ Cloudinary upload failed for user {current_user.id}: {result['error']}")
            return jsonify({
                "status": "error",
                "message": f"Upload failed: {result['error']}"
            }), 502

        mime_type = mimetypes.guess_type(safe_filename)[0] or "application/octet-stream"

        logger.info(
            f"✅ Attachment uploaded by user {current_user.id}: "
            f"{safe_filename} → {result['url']}"
        )

        return jsonify({
            "status": "success",
            "data": {
                "url": result["url"],
                "public_id": result.get("public_id"),
                "filename": file.filename,
                "size": file_size,
                "mime_type": mime_type,
                "file_category": file_category
            }
        }), 201

    except Exception as e:
        logger.error(f"❌ Attachment upload error: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


# -----------------------------------------------------------
# STATS & DIAGNOSTICS
# -----------------------------------------------------------

@learnora_bp.route("/api/stats", methods=["GET"])
@token_required
def get_stats(current_user):
    """
    Fetch provider stats and the current user's daily quota usage.
    """
    try:
        quota = AIUsageQuota.query.filter_by(user_id=current_user.id).first()

        daily_limit = quota.daily_messages_limit if quota else 10
        daily_used = quota.daily_messages_used if quota else 0

        return jsonify({
            "status": "success",
            "data": {
                "provider_stats": provider_manager.get_stats(),
                "user_quota": {
                    "daily_used": daily_used,
                    "daily_limit": daily_limit,
                    "remaining": max(0, daily_limit - daily_used),
                    "reset_date": quota.last_reset_date.isoformat() if quota and quota.last_reset_date else None
                }
            }
        })
    except Exception as e:
        logger.error(f"Error getting stats: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500
