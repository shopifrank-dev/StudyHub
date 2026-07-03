/**
 * Message System Rendering — PRODUCTION
 * All DOM updates for the messaging UI.
 * Uses messageState.getCurrentUserId() — never window globals.
 */

import * as messageState from './message.state.js';
import * as templates from './message.templates.js';
import {
  scrollToBottom,
  isScrolledToBottom,
  groupMessagesByDate,
} from './message.utils.js';

// ============================================================================
// CONVERSATION LIST
// ============================================================================

export function renderConversationList() {
  const container = document.getElementById('conversations-list');
  if (!container) return;

  if (messageState.messageState.isLoadingConversations) {
    container.innerHTML = templates.conversationSkeletonTemplate();
    return;
  }

  const conversations = messageState.getConversations();

  if (conversations.length === 0) {
    container.innerHTML = templates.emptyConversationsTemplate();
    return;
  }

  container.innerHTML = conversations
    .map(conv => templates.conversationItemTemplate(conv))
    .join('');
}

export function updateConversationItem(partnerId) {
  const conversation = messageState.getConversations().find(c => c.partner.id === partnerId);
  if (!conversation) return;

  const existing = document.querySelector(`.conversation-item[data-partner-id="${partnerId}"]`);
  if (existing) {
    const tmp = document.createElement('div');
    tmp.innerHTML = templates.conversationItemTemplate(conversation);
    existing.replaceWith(tmp.firstElementChild);
  } else {
    renderConversationList();
  }
}

// ============================================================================
// MESSAGE LIST
// ============================================================================

export function renderMessageList() {
  const el = document.getElementById('conversation-list-view');
  
  const container = document.getElementById('messages-list');
  if (!container) return;

  const currentUserId = messageState.getCurrentUserId();
  const messages = messageState.getMessages();

  if (messageState.messageState.isLoadingMessages && messages.length === 0) {
    container.innerHTML = templates.messageSkeletonTemplate();
    return;
  }

  if (messages.length === 0) {
    const partner = messageState.getCurrentPartner();
    container.innerHTML = templates.emptyMessagesTemplate(partner?.name || 'your contact');
    return;
  }

  const wasAtBottom = isScrolledToBottom(container);
  const groups      = groupMessagesByDate(messages);

  container.innerHTML = groups.map(group => `
    ${templates.dateSeparatorTemplate(group.date)}
    ${group.messages.map(msg => templates.messageTemplate(msg, currentUserId)).join('')}
  `).join('');

  if (wasAtBottom) scrollToBottom(container, false);
}

export function renderNewMessage(message) {
  const container = document.getElementById('messages-list');
  if (!container) return;

  const currentUserId = messageState.getCurrentUserId();
  const wasAtBottom   = isScrolledToBottom(container);

  // Remove empty-state if present
  const emptyState = container.querySelector('.empty-state, [class*="flex-col items-center"]');
  if (emptyState) emptyState.remove();

  // Date separator if needed
  const lastMsgEl   = container.querySelector('.message-wrapper:last-of-type');
  const lastDateStr = lastMsgEl?.dataset.sentAt;
  const newDateStr  = new Date(message.sent_at).toDateString();

  if (!lastDateStr || new Date(lastDateStr).toDateString() !== newDateStr) {
    container.insertAdjacentHTML('beforeend', templates.dateSeparatorTemplate(newDateStr));
  }

  container.insertAdjacentHTML('beforeend', templates.messageTemplate(message, currentUserId));

  if (wasAtBottom) scrollToBottom(container, true);
}

export function updateMessageInUI(clientTempId, serverMessage) {
  const messageEl     = document.querySelector(`[data-temp-id="${clientTempId}"]`);
  if (!messageEl) return;

  const currentUserId = messageState.getCurrentUserId();
  const tmp           = document.createElement('div');
  tmp.innerHTML       = templates.messageTemplate(serverMessage, currentUserId);

  const newEl = tmp.firstElementChild;
  messageEl.replaceWith(newEl);
}

/**
 * Soft-delete: keep element in DOM but replace content with "This message was deleted".
 * Called for both DELETE_FOR_ME and DELETE_FOR_EVERYONE.
 */
export function markMessageAsDeleted(messageId) {
  const messageEl = document.querySelector(`[data-message-id="${messageId}"]`);
  if (!messageEl) return;

  // Already deleted
  if (messageEl.dataset.isDeleted === 'true') return;

  const currentUserId = messageState.getCurrentUserId();
  const isOwn = messageEl.dataset.isOwn === 'true' ||
                messageEl.dataset.senderId === String(currentUserId);

  const tmp = document.createElement('div');
  tmp.innerHTML = templates.deletedMessageTemplate(
    { id: messageId, sender_id: isOwn ? currentUserId : 0, sent_at: new Date().toISOString() },
    currentUserId
  );
  messageEl.replaceWith(tmp.firstElementChild);
}

/**
 * Hard-remove (used when "delete for me" truly hides the message from local view only).
 * Falls back to markMessageAsDeleted to avoid broken UI.
 */
export function removeMessageFromUI(messageId) {
  // Prefer soft-delete so the conversation thread stays coherent.
  markMessageAsDeleted(messageId);
}

// ============================================================================
// CONVERSATION HEADER
// ============================================================================

export function renderConversationHeader() {
  const header = document.querySelector('.conversation-header');
  if (!header) return;

  const partner  = messageState.getCurrentPartner();
  if (!partner) return;

  const isOnline = messageState.isUserOnline(partner.id);
  header.innerHTML = templates.conversationHeaderTemplate(partner, isOnline);
}

export function updatePartnerStatus(partnerId, isOnline) {
  if (messageState.getCurrentPartnerId() !== partnerId) return;

  const statusText = document.querySelector('.partner-status-text');
  const statusDot  = document.querySelector('.conversation-header .rounded-full.border-2');

  if (statusText) statusText.textContent = isOnline ? 'Online' : 'Offline';
  if (statusDot) {
    statusDot.classList.toggle('bg-[var(--success)]', isOnline);
    statusDot.classList.toggle('bg-[var(--text-tertiary)]', !isOnline);
  }
}

// ============================================================================
// BLOCKED STATE
// ============================================================================

/**
 * Show the blocked notice and HIDE (not destroy) the real toolbar.
 * This preserves #message-input, #msg-send-btn, and all IDs in the DOM.
 */
export function renderBlockedNotice(type) {
  const toolbar = document.getElementById('message-toolbar');
  const notice  = document.getElementById('message-blocked-notice');
  const partner = messageState.getCurrentPartner();
  if (!notice || !partner) return;

  // Fill in the correct notice HTML
  notice.innerHTML = type === 'blocked_by_me'
    ? templates.blockedByMeNoticeTemplate(partner.name)
    : templates.blockedByPartnerNoticeTemplate(partner.name);

  // Show notice, hide real toolbar
  notice.classList.remove('hidden');
  toolbar?.classList.add('hidden');
}

/**
 * Restore the real input toolbar after unblocking.
 * Just reverses the hide/show — no innerHTML replacement needed.
 */
export function restoreMessageInput() {
  const toolbar = document.getElementById('message-toolbar');
  const notice  = document.getElementById('message-blocked-notice');

  notice?.classList.add('hidden');
  notice && (notice.innerHTML = '');   // clear stale content
  toolbar?.classList.remove('hidden');

  // Re-evaluate send button state
  updateSendMicToggle();
}

// ============================================================================
// TYPING INDICATOR
// ============================================================================

export function showTypingIndicator() {
  const container = document.getElementById('messages-list');
  if (!container) return;

  const existing = container.querySelector('.typing-indicator');
  if (existing) return; // already shown

  container.insertAdjacentHTML('beforeend', templates.typingIndicatorTemplate());
  scrollToBottom(container, true);
}

export function hideTypingIndicator() {
  document.querySelector('.typing-indicator')?.remove();
}

// ============================================================================
// PENDING ATTACHMENTS PREVIEW
// ============================================================================


export function renderPendingAttachments() {
  const strip = document.getElementById('pending-attachments-strip');
  if (!strip) return;

  const inner = strip.querySelector('.no-scrollbar') ?? strip;
  const attachments = messageState.getPendingAttachments();

  if (attachments.length === 0) {
    inner.innerHTML = '';
    strip.classList.add('hidden');
    return;
  }

  strip.classList.remove('hidden');
  inner.innerHTML = attachments
    .map(a => templates.pendingAttachmentPreviewTemplate(a))
    .join('');
}

// ============================================================================
// SEND BUTTON / MIC TOGGLE
// ============================================================================

/**
 * Show send button when there's text or attachments; hide it when input is empty
 * (mic button has been removed — send button is always the right-side action).
 */
export function updateSendMicToggle() {
  const input   = document.getElementById('message-input');
  const sendBtn = document.getElementById('msg-send-btn');
  if (!input || !sendBtn) return;

  const hasText        = input.value.trim().length > 0;
  const hasAttachments = messageState.getPendingAttachments().length > 0;

  // Always show send button; just dim it when nothing to send
  sendBtn.classList.remove('hidden');
  sendBtn.disabled = !hasText && !hasAttachments;
  sendBtn.classList.toggle('opacity-50', !hasText && !hasAttachments);
  sendBtn.classList.toggle('cursor-not-allowed', !hasText && !hasAttachments);
}

// ============================================================================
// UPLOAD PROGRESS
// ============================================================================

export function updateAttachmentProgress(fileId, percent) {
  const overlay = document.querySelector(`[data-upload-id="${fileId}"]`);
  if (!overlay) return;

  overlay.classList.remove('hidden');
  overlay.classList.add('flex');
  const label = overlay.querySelector('span');
  if (label) label.textContent = `${Math.round(percent)}%`;

  if (percent >= 100) {
    setTimeout(() => {
      overlay.classList.add('hidden');
      overlay.classList.remove('flex');
    }, 500);
  }
}

// ============================================================================
// OFFLINE BANNERS
// ============================================================================

export function showOfflineBanner() {
  if (document.querySelector('.offline-banner')) return;
  const section = document.getElementById('messages');
  section?.insertAdjacentHTML('afterbegin', templates.offlineBannerTemplate());
}

export function hideOfflineBanner() {
  document.querySelector('.offline-banner')?.remove();
}

export function showReconnectingBanner() {
  if (document.querySelector('.reconnecting-banner')) return;
  const section = document.getElementById('messages');
  section?.insertAdjacentHTML('afterbegin', templates.reconnectingBannerTemplate());
}

export function hideReconnectingBanner() {
  document.querySelector('.reconnecting-banner')?.remove();
}

// ============================================================================
// IMAGE VIEWER
// ============================================================================

export function openImageViewer(url, filename) {
  const modal = document.getElementById('msg-image-viewer-modal');
  modal.dataset.url = url;
  if (!modal) return;

  const img  = modal.querySelector('#msg-viewer-img');
  const name = modal.querySelector('#msg-viewer-filename');

  if (img)  img.src = url;
  if (name) name.textContent = filename || '';

  modal.classList.remove('hidden');
  modal.classList.add('flex');
  document.body.style.overflow = 'hidden';
}

export function closeImageViewer() {
  const modal = document.getElementById('msg-image-viewer-modal');
  if (!modal) return;
  modal.classList.add('hidden');
  modal.classList.remove('flex');
  document.body.style.overflow = '';
}


