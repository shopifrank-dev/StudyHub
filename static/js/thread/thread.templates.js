/**
 * thread.templates.js — Tailwind edition
 * Pure HTML string templates for the thread system.
 *
 * CHANGES:
 *  - NEW: renderMessageText() exported — mention highlighting + code blocks
 *    with syntax highlighting via highlight.js (window.hljs).
 *  - Issue 1: _buildAttachmentHtml renders +X overlay when >2 images.
 *  - Issue 1: _renderAttachmentItem images include ⬇ download overlay.
 *  - Issue 3: msg-options-btn CSS-only visibility (no Tailwind opacity classes).
 *  - NEW: msg-quick-reply-btn on theirs messages (desktop hover via CSS).
 *  - threadListItemTemplate: dept badge + closed indicator added.
 *  - BUG FIX: whitespace-pre-wrap removed from msg-text (renderMessageText
 *    converts \n to <br> so the span renders correctly without it).
 */

import { MSG_STATUS, AI_BOT_TRIGGERS } from './thread.constants.js';


// ─── Utilities ────────────────────────────────────────────────────────────────

function esc(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function escAttr(str) {
  return String(str ?? '').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function _formatTime(isoString) {
  if (!isoString) return '';
  const d   = new Date(isoString);
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth()    === now.getMonth()    &&
    d.getDate()     === now.getDate();

  if (sameDay) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

  const msPerDay = 86400000;
  const daysDiff = Math.floor((now - d) / msPerDay);
  if (daysDiff < 7) {
    return `${d.toLocaleDateString([], { weekday: 'short' })} ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
  }
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function _timeAgo(isoString) {
  if (!isoString) return '';
  const delta = Math.floor((Date.now() - new Date(isoString).getTime()) / 1000);
  if (delta < 60)    return 'just now';
  if (delta < 3600)  return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  return `${Math.floor(delta / 86400)}d ago`;
}


// ─── Status icon ──────────────────────────────────────────────────────────────

export function statusIconTemplate(status) {
  switch (status) {
    case MSG_STATUS.PENDING:
      return `<svg class="status-icon status-pending opacity-50" viewBox="0 0 16 16" width="14" height="14">
        <circle cx="8" cy="8" r="6" stroke="currentColor" fill="none" stroke-width="1.5" stroke-dasharray="2 2"/>
      </svg>`;
    case MSG_STATUS.FAILED:
      return `<svg class="status-icon status-failed text-red-300" viewBox="0 0 16 16" width="14" height="14">
        <circle cx="8" cy="8" r="6" stroke="currentColor" fill="none" stroke-width="1.5"/>
        <line x1="8" y1="5" x2="8" y2="9" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
        <circle cx="8" cy="11" r="0.7" fill="currentColor"/>
      </svg>`;
    case MSG_STATUS.SENT:
      return `<svg class="status-icon status-sent opacity-70" viewBox="0 0 16 11" width="14" height="10">
        <path d="M1.5 5.5 L5.5 9.5 L14.5 1.5" stroke="currentColor" fill="none" stroke-width="1.6" stroke-linecap="round"/>
      </svg>`;
    case MSG_STATUS.DELIVERED:
      return `<svg class="status-icon status-delivered opacity-70" viewBox="0 0 20 11" width="18" height="10">
        <path d="M1.5 5.5 L5.5 9.5 L14.5 1.5" stroke="currentColor" fill="none" stroke-width="1.6" stroke-linecap="round"/>
        <path d="M6.5 5.5 L10.5 9.5 L19.5 1.5" stroke="currentColor" fill="none" stroke-width="1.6" stroke-linecap="round"/>
      </svg>`;
    case MSG_STATUS.READ:
      return `<svg class="status-icon status-read" viewBox="0 0 20 11" width="18" height="10">
        <path d="M1.5 5.5 L5.5 9.5 L14.5 1.5" stroke="#a5b4fc" fill="none" stroke-width="1.6" stroke-linecap="round"/>
        <path d="M6.5 5.5 L10.5 9.5 L19.5 1.5" stroke="#a5b4fc" fill="none" stroke-width="1.6" stroke-linecap="round"/>
      </svg>`;
    default:
      return '';
  }
}


// ─── renderMessageText (exported) ─────────────────────────────────────────────

/**
 * Convert a plain-text message into safe HTML with:
 *  - Fenced code blocks  (```lang\ncode\n```) with hljs syntax highlighting
 *  - Inline code         (`code`)
 *  - @mention spans      (coloured by AI vs. human)
 *  - Newlines → <br>     in plain-text segments
 *
 * Security: all non-code text is run through esc(). Code is passed to
 * highlight.js which handles its own escaping; the fallback is esc() too.
 *
 * @param {string} text
 * @returns {string} safe HTML string
 */
export function renderMessageText(text) {
  if (!text) return '';

  const result = [];
  // Match fenced code blocks, inline code, or @mentions — in that priority order.
  const TOKEN = /```([a-zA-Z0-9]*)\n?([\s\S]*?)```|`([^`\n]+)`|(@[a-zA-Z0-9_]{1,30})/g;
  let lastIndex = 0;
  let match;

  while ((match = TOKEN.exec(text)) !== null) {
    // Plain text before this token
    if (match.index > lastIndex) {
      result.push(_escapeAndBreak(text.slice(lastIndex, match.index)));
    }

    if (match[1] !== undefined) {
      // Fenced code block
      result.push(_renderCodeBlock(match[1], match[2] ?? ''));
    } else if (match[3] !== undefined) {
      // Inline code
      result.push(
        `<code class="msg-inline-code bg-black/10 rounded px-1 py-0.5 text-[0.85em] font-mono">${esc(match[3])}</code>`
      );
    } else if (match[4] !== undefined) {
      // @mention
      const raw      = match[4];
      const username = esc(raw.slice(1));
      const isBot    = AI_BOT_TRIGGERS.includes(raw.slice(1).toLowerCase());
      const cls      = isBot
        ? 'text-violet-700 bg-violet-100 hover:bg-violet-200'
        : 'text-indigo-700 bg-indigo-100 hover:bg-indigo-200';
      result.push(
        `<span class="mention font-semibold rounded px-0.5 cursor-default ${cls}" data-mention="${username}">@${username}</span>`
      );
    }

    lastIndex = TOKEN.lastIndex;
  }

  // Remaining plain text
  if (lastIndex < text.length) {
    result.push(_escapeAndBreak(text.slice(lastIndex)));
  }

  return result.join('');
}

/** Escape HTML entities and convert \n to <br> for plain-text segments. */
function _escapeAndBreak(str) {
  return esc(str).replace(/\n/g, '<br>');
}

/**
 * Render a fenced code block with syntax highlighting.
 * Uses window.hljs if available, falls back to escaped plain text.
 * Each block gets a unique ID so the copy button can read its textContent.
 */
function _renderCodeBlock(lang, code) {
  const trimmed   = code.trimEnd();
  const langLabel = lang ? esc(lang) : 'code';
  const blockId   = `cb-${Math.random().toString(36).slice(2, 9)}`;

  let highlighted;
  if (window.hljs) {
    try {
      if (lang && window.hljs.getLanguage(lang)) {
        highlighted = window.hljs.highlight(trimmed, { language: lang }).value;
      } else {
        highlighted = window.hljs.highlightAuto(trimmed).value;
      }
    } catch {
      highlighted = esc(trimmed);
    }
  } else {
    highlighted = esc(trimmed);
  }

  return `
    <div class="msg-code-block my-1.5 rounded-xl overflow-hidden text-[12px] border border-white/10">
      <div class="flex items-center justify-between px-3 py-1.5 bg-gray-900 text-gray-400">
        <span class="text-[10px] font-mono">${langLabel}</span>
        <button class="msg-copy-code-btn flex items-center gap-1 text-[10px]
                       hover:text-white transition-colors px-1.5 py-0.5 rounded"
                data-action="thread-copy-code"
                data-code-id="${blockId}"
                aria-label="Copy code">
          📋 Copy
        </button>
      </div>
      <pre id="${blockId}"
           class="hljs overflow-x-auto p-3 bg-gray-900 text-gray-100 m-0
                  max-h-72 leading-relaxed text-[12px]"><code class="language-${esc(lang || 'plaintext')}">${highlighted}</code></pre>
    </div>`;
}


// ─── Attachment helpers ───────────────────────────────────────────────────────

/**
 * Render a single attachment item (image / video / document).
 * Images include a ⬇ download overlay on hover.
 */
function _renderAttachmentItem(att, isMine, compact = false) {
  const aType  = att.attachment_type ?? '';
  const aName  = esc(att.attachment_name ?? 'Attachment');
  const aUrl   = escAttr(att.attachment_url ?? '');
  const sizeKb = att.attachment_size ? ` (${Math.round(att.attachment_size / 1024)} KB)` : '';

  if (aType === 'image') {
    const imgCls = compact
      ? 'w-full h-28 object-cover rounded-lg'
      : 'max-w-[220px] max-h-[220px] object-cover rounded-xl';
    return `
      <div class="relative group/img">
        <a href="${aUrl}" target="_blank" rel="noopener noreferrer" class="block">
          <img src="${aUrl}" class="msg-attachment-image ${imgCls}" loading="lazy" alt="${aName}">
        </a>
        <a href="${aUrl}" download="${aName}" target="_blank" rel="noopener noreferrer"
           class="absolute bottom-1.5 right-1.5 w-7 h-7 rounded-lg bg-black/50 text-white
                  flex items-center justify-center text-xs opacity-0 group-hover/img:opacity-100
                  transition-opacity hover:bg-black/70"
           title="Download ${aName}" aria-label="Download">⬇</a>
      </div>`;
  }

  if (aType === 'video') {
    return `<video src="${aUrl}" class="msg-attachment-video rounded-xl max-w-[220px]"
                   controls preload="metadata"></video>`;
  }

  // Document / other
  const fileBg = isMine
    ? 'bg-indigo-500/30 hover:bg-indigo-500/40'
    : 'bg-gray-200 hover:bg-gray-300';
  return `
    <a href="${aUrl}" target="_blank" rel="noopener noreferrer" download
       class="flex items-center gap-2 rounded-xl px-3 py-2 ${fileBg} transition-colors">
      <span class="text-base">📎</span>
      <span class="text-sm font-medium truncate max-w-[160px]">${aName}</span>
      <span class="text-xs opacity-70">${sizeKb}</span>
    </a>`;
}

/**
 * Build the full attachment HTML block for a message.
 * Caps inline images at 2; shows +N overlay on the second if more exist.
 * Adds download overlay on hover for all images.
 */
function _buildAttachmentHtml(message, isMine) {
  if (message.is_deleted) return '';

  const attachments = Array.isArray(message.attachments) && message.attachments.length
    ? message.attachments
    : message.attachment_url
      ? [{
          attachment_url:  message.attachment_url,
          attachment_name: message.attachment_name,
          attachment_type: message.attachment_type,
          attachment_size: message.attachment_size,
        }]
      : [];

  if (!attachments.length) return '';

  const INLINE_MAX = 2;
  const images     = attachments.filter((a) => a.attachment_type === 'image');
  const others     = attachments.filter((a) => a.attachment_type !== 'image');
  const extraCount = images.length - INLINE_MAX;

  let html = '';

  if (images.length === 1) {
    html += `<div class="mb-1.5">${_renderAttachmentItem(images[0], isMine, false)}</div>`;
  } else if (images.length >= 2) {
    const secondHtml = extraCount > 0
      ? `<div class="relative">
           ${_renderAttachmentItem(images[1], isMine, true)}
           <button class="absolute inset-0 flex items-center justify-center
                          bg-black/50 rounded-lg text-white text-lg font-bold
                          hover:bg-black/60 transition-colors"
                   data-action="thread-open-attachments"
                   aria-label="${extraCount} more attachments">+${extraCount}</button>
         </div>`
      : _renderAttachmentItem(images[1], isMine, true);

    html += `<div class="grid grid-cols-2 gap-1 mb-1 max-w-[220px]">
      ${_renderAttachmentItem(images[0], isMine, true)}
      ${secondHtml}
    </div>`;
  }

  if (others.length) {
    html += others.map((a) => `<div class="mb-1">${_renderAttachmentItem(a, isMine, false)}</div>`).join('');
  }

  return html;
}


// ─── Message template ─────────────────────────────────────────────────────────

export function threadMessageTemplate(message, currentUserId) {
  const isMine     = message.sender_id === currentUserId;
  const sender     = message.sender ?? {};
  const senderName = esc(sender.name ?? 'Unknown');
  const avatarUrl  = sender.avatar ? escAttr(sender.avatar) : null;
  const time       = _formatTime(message.sent_at);
  const hasId      = message.id != null;

  // Determine AI personality display name
  const aiPersonality = message.ai_personality
    ? message.ai_personality.charAt(0).toUpperCase() + message.ai_personality.slice(1)
    : 'Learnora';

  const wrapClass = [
    'thread-message-wrap group relative flex gap-2 px-3 py-0.5',
    isMine ? 'justify-end mine' : 'justify-start items-end theirs',
    !hasId || message.status === MSG_STATUS.PENDING ? 'message-pending opacity-70' : 'message-confirmed',
    message.is_deleted     ? 'message-deleted' : '',
    message.is_pinned      ? 'message-pinned'  : '',
    message.is_ai_response ? 'message-ai'      : '',
  ].filter(Boolean).join(' ');

  const bubbleColClass = isMine
    ? 'msg-bubble-col flex flex-col items-end gap-0 max-w-[78%]'
    : 'msg-bubble-col flex flex-col items-start gap-0 max-w-[78%]';

  const bubbleClass = isMine
    ? 'msg-bubble bg-indigo-600 text-white rounded-2xl rounded-br-sm px-3.5 py-2.5 select-none'
    : 'msg-bubble bg-gray-100 text-gray-900 rounded-2xl rounded-bl-sm px-3.5 py-2.5 select-none';

  const tempIdAttr = message.client_temp_id ? ` data-temp-id="${escAttr(message.client_temp_id)}"` : '';
  const msgIdAttr  = hasId ? ` data-message-id="${message.id}"` : '';

  // ── Avatar ────────────────────────────────────────────────────────────────
  const avatarHtml = avatarUrl
    ? `<img src="${avatarUrl}" class="msg-avatar w-8 h-8 rounded-full object-cover flex-shrink-0"
            alt="${senderName}" loading="lazy">`
    : `<div class="msg-avatar-placeholder flex-shrink-0 w-8 h-8 rounded-full bg-indigo-100
                   text-indigo-700 text-xs font-bold flex items-center justify-center select-none">
         ${(sender.name ?? '?').charAt(0).toUpperCase()}
       </div>`;

  // ── Reply preview ─────────────────────────────────────────────────────────
  let replyHtml = '';
  if (message.reply_to) {
    const rt        = message.reply_to;
    const replyBg   = isMine ? 'bg-indigo-500/30 border-white/50' : 'bg-gray-200 border-indigo-400';
    const nameColor = isMine ? 'text-indigo-100' : 'text-indigo-600';
    const textColor = isMine ? 'text-white/80'   : 'text-gray-500';
    replyHtml = `
      <div class="msg-reply-preview rounded-lg px-2.5 py-1.5 mb-1.5 border-l-2 ${replyBg}
                  cursor-pointer" data-action="thread-scroll-to-message"
           data-message-id="${rt.id}">
        <span class="reply-sender block text-xs font-semibold ${nameColor}">${esc(rt.sender ?? '')}</span>
        <span class="reply-text text-xs ${textColor} line-clamp-1">${esc((rt.text ?? '').slice(0, 80))}</span>
      </div>`;
  }

  // ── Attachments (multi-file aware, +X truncation) ─────────────────────────
  const attachmentHtml = _buildAttachmentHtml(message, isMine);

  // ── Text — rendered via renderMessageText for rich formatting ─────────────
  const textHtml = message.is_deleted
    ? `<span class="msg-text italic opacity-50 text-sm">[deleted]</span>`
    : message.text_content
      ? `<span class="msg-text text-sm leading-relaxed break-words">${renderMessageText(message.text_content)}</span>`
      : '';

  // ── Edited label ──────────────────────────────────────────────────────────
  const editedHtml = message.is_edited && !message.is_deleted
    ? `<span class="msg-edited-label text-[10px] opacity-60 ml-1">edited</span>`
    : '';

  // ── Reactions ─────────────────────────────────────────────────────────────
  const reactions = message.reactions ?? {};
  const rxnPills  = Object.values(reactions).map((r) => {
    const mine = Array.isArray(r.users) && r.users.includes(currentUserId);
    return `<button class="reaction-pill flex items-center gap-1 text-xs rounded-full px-2 py-0.5
                     transition-colors ${mine
                       ? 'bg-indigo-100 text-indigo-700 ring-1 ring-indigo-300'
                       : 'bg-gray-100 text-gray-700 hover:bg-gray-200'}"
               data-action="thread-react"
               data-message-id="${message.id}"
               data-emoji="${escAttr(r.emoji)}">
               ${esc(r.emoji)} <span>${r.count}</span>
             </button>`;
  }).join('');
  const reactionsHtml = rxnPills
    ? `<div class="msg-reactions flex flex-wrap gap-1 mt-1">${rxnPills}</div>`
    : '';

  // ── Status tick ───────────────────────────────────────────────────────────
  const statusHtml = isMine
    ? `<span class="msg-status-icon inline-flex items-center opacity-80">
         ${statusIconTemplate(message.status ?? MSG_STATUS.SENT)}
       </span>`
    : '';

  // ── Pin badge ─────────────────────────────────────────────────────────────
  const pinBadge = message.is_pinned
    ? `<span class="msg-pin-icon text-xs text-amber-500 mr-1" title="Pinned">📌</span>`
    : `<span class="msg-pin-icon hidden"></span>`;

  // ── AI badge ──────────────────────────────────────────────────────────────
  const aiBadge = message.is_ai_response
    ? `<span class="msg-ai-badge inline-flex items-center gap-1 text-xs font-medium
                    text-violet-700 bg-violet-100 rounded-full px-2 py-0.5 mb-1 self-start">
         🤖 ${esc(aiPersonality)}
       </span>`
    : '';

  // ── Retry button ──────────────────────────────────────────────────────────
  const retryHtml = message.status === MSG_STATUS.FAILED
    ? `<button class="msg-retry-btn text-xs text-red-500 hover:text-red-700 underline mt-0.5 px-1"
               data-action="thread-retry"
               data-temp-id="${escAttr(message.client_temp_id ?? '')}">
         Retry
       </button>`
    : '';

  // ── Options button (Issue 3: CSS-only visibility) ─────────────────────────
  const optionsBtn = !message.is_deleted && hasId
    ? `<button class="msg-options-btn absolute ${isMine ? 'left-0 -translate-x-full' : 'right-0 translate-x-full'}
                      top-0 w-7 h-7 rounded-full bg-white shadow-sm border border-gray-200 text-gray-500
                      hover:text-indigo-600 hover:border-indigo-300 flex items-center justify-center
                      text-xs select-none"
               data-action="thread-open-options"
               data-message-id="${message.id}"
               aria-label="Message options">
         ⋯
       </button>`
    : '';

  // ── Quick-reply button (theirs messages, desktop hover-only via CSS) ──────
  const quickReplyBtn = !isMine && !message.is_deleted && hasId
    ? `<button class="msg-quick-reply-btn absolute right-0 translate-x-full top-0
                       w-7 h-7 rounded-full bg-white shadow-sm border border-gray-200
                       text-gray-500 hover:text-indigo-600 flex items-center justify-center
                       text-xs select-none"
               data-action="thread-reply"
               data-message-id="${message.id}"
               aria-label="Reply">↩</button>`
    : '';

  // ── Layout ────────────────────────────────────────────────────────────────
  return `
    <div class="${wrapClass}"${msgIdAttr}${tempIdAttr}>
      ${!isMine ? `<div class="flex-shrink-0">${avatarHtml}</div>` : ''}
      <div class="${bubbleColClass} relative">
        ${optionsBtn}
        ${quickReplyBtn}
        ${!isMine ? `<span class="msg-sender-name text-[11px] font-semibold text-gray-500 px-1 mb-0.5">${senderName}</span>` : ''}
        ${aiBadge}
        <div class="${bubbleClass}">
          ${pinBadge}
          ${replyHtml}
          ${attachmentHtml}
          ${textHtml}
          ${editedHtml}
        </div>
        ${reactionsHtml}
        <div class="msg-meta flex items-center gap-1.5 px-1 mt-0.5">
          <span class="msg-time text-[10px] text-gray-400">${time}</span>
          ${statusHtml}
          ${retryHtml}
        </div>
      </div>
    </div>`;
}


// ─── Thread list item ─────────────────────────────────────────────────────────

export function threadListItemTemplate(thread, currentUserId) {
  const unread  = thread.unread_count ?? 0;
  const lastMsg = thread.last_message;

  let previewText = '';
  let previewTime = '';

  if (lastMsg) {
    const isSelf = lastMsg.sender_id === currentUserId;
    const prefix = isSelf
      ? ''
      : lastMsg.sender ? `${lastMsg.sender.split(' ')[0]}: ` : '';
    previewText = prefix + (lastMsg.text ?? '').slice(0, 55);
    previewTime = _timeAgo(lastMsg.sent_at);
  } else {
    previewText = thread.description
      ? thread.description.slice(0, 55) + (thread.description.length > 55 ? '…' : '')
      : 'No messages yet';
    previewTime = _timeAgo(thread.last_activity);
  }

  const avatarHtml = thread.avatar
    ? `<img src="${escAttr(thread.avatar)}"
            class="thread-avatar w-12 h-12 rounded-full object-cover flex-shrink-0"
            alt="${esc(thread.title)}" loading="lazy">`
    : `<div class="thread-avatar-placeholder w-12 h-12 rounded-full bg-accent-subtle text-accent
                    text-base font-bold flex items-center justify-center flex-shrink-0 select-none">
         ${esc(thread.title.charAt(0).toUpperCase())}
       </div>`;

  const unreadPill = unread > 0
    ? `<span class="thread-unread-badge min-w-[20px] h-5 rounded-full bg-accent text-white
                    text-[10px] font-bold flex items-center justify-center px-1.5 leading-none self-end">
         ${unread > 99 ? '99+' : unread}
       </span>`
    : `<span class="thread-unread-badge hidden" aria-hidden="true"></span>`;

  const isSelfMsg  = lastMsg && lastMsg.sender_id === currentUserId;
  const statusIcon = isSelfMsg ? statusIconTemplate(lastMsg.status) : '';

  // Dept badge
  const deptBadge = thread.department
    ? `<span class="text-[9px] font-semibold text-accent bg-accent-subtle rounded-full
                    px-1.5 py-0.5 flex-shrink-0 leading-tight">
         ${esc(thread.department)}
       </span>`
    : '';

  // Closed badge
  const closedBadge = !thread.is_open
    ? `<span class="text-[9px] text-red-400 font-semibold ml-0.5">🔒</span>`
    : '';

  return `
    <div class="thread-list-item flex items-center gap-3 px-4 py-3 bg-surface-card
                 hover:bg-surface-hover active:bg-surface-hover transition-colors cursor-pointer
                 border-b border-line-light"
         data-action="open-thread"
         data-thread-id="${thread.id}"
         role="button" tabindex="0"
         aria-label="Open ${esc(thread.title)}">

      <div class="flex-shrink-0">${avatarHtml}</div>

      <div class="flex-1 min-w-0">
        <div class="flex items-center gap-1.5 mb-0.5 flex-wrap">
          <span class="text-sm font-semibold text-ink-primary truncate">${esc(thread.title)}</span>
          ${deptBadge}
          ${closedBadge}
        </div>
        <div class="flex items-center gap-1 min-w-0">
          ${statusIcon}
          <span class="thread-last-message text-xs text-ink-secondary truncate">${esc(previewText)}</span>
        </div>
      </div>

      <div class="flex flex-col items-end gap-1 flex-shrink-0 self-start pt-0.5">
        <span class="text-[11px] text-ink-tertiary whitespace-nowrap">${previewTime}</span>
        ${unreadPill}
      </div>

    </div>`;
}


// ─── Pinned messages banner ───────────────────────────────────────────────────

export function pinnedMessagesBannerTemplate(pinnedMessages) {
  if (!pinnedMessages?.length) return '';

  const count       = pinnedMessages.length;
  const first       = pinnedMessages[0];
  const firstText   = esc((first.text_content ?? '📎 Attachment').slice(0, 80));
  const firstSender = esc(first.sender?.name ?? '');

  const pinsJson = escAttr(JSON.stringify(
    pinnedMessages.map((p) => ({
      id:     p.id,
      text:   (p.text_content ?? '📎 Attachment').slice(0, 80),
      sender: p.sender?.name ?? '',
    }))
  ));

  return `
    <div class="thread-pinned-banner flex items-center gap-2 px-3 py-2
                bg-amber-50 border-b border-amber-100"
         data-pin-index="0" data-pin-count="${count}" data-pins="${pinsJson}">

      <button class="pin-icon-btn text-base flex-shrink-0 hover:scale-110 transition-transform"
              data-action="thread-scroll-to-message" data-message-id="${first.id}"
              aria-label="Jump to pinned message">📌</button>

      <div class="pin-content flex-1 min-w-0 cursor-pointer"
           data-action="thread-scroll-to-message" data-message-id="${first.id}">
        ${count > 1
          ? `<span class="text-[10px] font-bold text-amber-600 uppercase tracking-wide">
               ${count} pinned
             </span>`
          : ''}
        <p class="pin-sender text-xs font-semibold text-amber-800 leading-tight">${firstSender}</p>
        <p class="pin-text text-xs text-gray-600 truncate">${firstText}</p>
      </div>

      ${count > 1 ? `
      <div class="flex flex-col gap-0.5">
        <button class="pin-nav-btn w-5 h-5 rounded flex items-center justify-center text-[10px]
                       text-gray-400 hover:text-amber-600 hover:bg-amber-100 transition-colors"
                data-pin-dir="-1" aria-label="Previous pin">▲</button>
        <button class="pin-nav-btn w-5 h-5 rounded flex items-center justify-center text-[10px]
                       text-gray-400 hover:text-amber-600 hover:bg-amber-100 transition-colors"
                data-pin-dir="1" aria-label="Next pin">▼</button>
      </div>` : ''}

      <button class="flex-shrink-0 text-xs font-semibold text-amber-700 hover:text-amber-900
                     px-2 py-1 rounded hover:bg-amber-100 transition-colors"
              data-action="thread-open-pinned-list">All</button>
    </div>`;
}


// ─── Search result item ───────────────────────────────────────────────────────

export function searchResultItemTemplate(result, query) {
  const senderName  = esc(result.sender?.name ?? 'Unknown');
  const time        = _formatTime(result.sent_at);
  const rawText     = result.text_content ?? '';
  const escaped     = esc(rawText.slice(0, 200));
  const highlighted = query
    ? escaped.replace(
        new RegExp(esc(query).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'),
        (m) => `<mark class="bg-indigo-100 text-indigo-800 rounded px-0.5 not-italic">${m}</mark>`
      )
    : escaped;

  return `
    <div class="px-4 py-3 hover:bg-indigo-50/40 active:bg-indigo-50 cursor-pointer"
         data-action="thread-scroll-to-message" data-message-id="${result.id}"
         role="button" tabindex="0">
      <div class="flex items-center justify-between mb-1">
        <span class="text-xs font-semibold text-gray-700">${senderName}</span>
        <span class="text-[11px] text-gray-400">${time}</span>
      </div>
      <p class="text-sm text-gray-600 leading-snug">${highlighted}</p>
    </div>`;
}


// ─── System message ───────────────────────────────────────────────────────────

export function systemMessageTemplate(text) {
  return `
    <div class="thread-system-message flex items-center justify-center py-2 px-4">
      <span class="text-xs text-gray-400 bg-gray-100 rounded-full px-3 py-1">${esc(text)}</span>
    </div>`;
}


// ─── Typing indicator ─────────────────────────────────────────────────────────

export function typingIndicatorTemplate(text) {
  return `
    <div id="thread-typing-indicator"
         class="thread-typing-indicator flex items-center gap-2 px-4 py-2">
      <div class="flex items-center gap-0.5">
        <span class="w-1.5 h-1.5 bg-gray-400 rounded-full animate-bounce"
              style="animation-delay:0ms"></span>
        <span class="w-1.5 h-1.5 bg-gray-400 rounded-full animate-bounce"
              style="animation-delay:150ms"></span>
        <span class="w-1.5 h-1.5 bg-gray-400 rounded-full animate-bounce"
              style="animation-delay:300ms"></span>
      </div>
      <span class="typing-text text-xs text-gray-400">${esc(text)}</span>
    </div>`;
}
