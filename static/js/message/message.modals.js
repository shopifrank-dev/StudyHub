/**
 * Message System Modals — PRODUCTION
 * Manages all modal open/close logic for the messaging system.
 * Works with the global openModal / closeModal utilities from the app.
 */

import * as messageState from './message.state.js';
import {
  renderConversationList,
  renderMessageList,
  renderConversationHeader,
  updateSendMicToggle,
} from './message.render.js';
import { reactionPickerTemplate } from './message.templates.js';

const _openModal  = () => window.openModal  ?? globalThis.openModal  ?? ((id) => document.getElementById(id)?.classList.remove('hidden'));
const _closeModal = () => window.closeModal ?? globalThis.closeModal ?? ((id) => document.getElementById(id)?.classList.add('hidden'));

const openModal  = (id) => (_openModal())(id);
const closeModal = (id) => (_closeModal())(id);

// ============================================================================
// CONVERSATION LIST ↔ ACTIVE CONVERSATION  (panel switching)
// ============================================================================

export function showConversationListView() {
  const listView = document.getElementById('conversation-list-view');
  const chatView = document.getElementById('active-conversation-view');

  chatView?.classList.add('hidden');     // ✅ hide chat
  listView?.classList.remove('hidden');  // ✅ show list

  messageState.clearCurrentConversation();
  renderConversationList();
}

export function showConversationView() {
  const listView = document.getElementById('conversation-list-view');
  const chatView = document.getElementById('active-conversation-view');

  listView?.classList.add('hidden');     // ✅ hide list
  chatView?.classList.remove('hidden'); // ✅ show chat

  renderConversationHeader();
  renderMessageList();
  updateSendMicToggle();
}
// ============================================================================
// REACTION PICKER
// ============================================================================

export function openReactionPicker(messageId, anchorEl) {
  messageState.showReactionPicker(messageId);

  const modal   = document.getElementById('msg-reaction-picker');
  const content = document.getElementById('msg-reaction-picker-content');
  if (!modal || !content) return;

  content.innerHTML = reactionPickerTemplate(messageId);
  modal.classList.remove('hidden');
  modal.classList.add('flex');
}

export function closeReactionPicker() {
  messageState.hideReactionPicker();
  const modal = document.getElementById('msg-reaction-picker');
  modal?.classList.add('hidden');
  modal?.classList.remove('flex');
}

// ============================================================================
// MESSAGE OPTIONS  (bottom sheet — triggered by long-press / right-click)
// ============================================================================

/**
 * @param {number}  messageId
 * @param {boolean} isOwn
 * @param {string}  sentAt  – ISO timestamp, used to compute delete-for-everyone eligibility
 */
export function openMessageOptionsModal(messageId, isOwn, sentAt) {
  messageState.showMessageOptions(messageId);

  const modal       = document.getElementById('msg-options-modal');
  const deleteEvery = document.getElementById('msg-opt-delete-everyone');
  const deleteMe    = document.getElementById('msg-opt-delete-me');
  const copyBtn     = document.getElementById('msg-opt-copy');

  if (!modal) return;

  // Show/hide "delete for everyone" based on ownership and time window
  if (deleteEvery) {
    const canDelEvery = isOwn && _canDeleteForEveryone(sentAt);
    deleteEvery.classList.toggle('hidden', !canDelEvery);
  }

  // "Delete for me" always available if there's a real messageId
  if (deleteMe) {
    deleteMe.classList.toggle('hidden', !messageId);
  }

  // Copy only meaningful for own messages with a body
  const message = messageState.getMessages().find(m => m.id === messageId);
  if (copyBtn) {
    copyBtn.classList.toggle('hidden', !message?.body);
  }

  modal.classList.remove('hidden');
  modal.classList.add('flex');
  // Trigger slide-up animation
  requestAnimationFrame(() => {
    modal.querySelector('.msg-options-sheet')?.classList.add('translate-y-0');
    modal.querySelector('.msg-options-sheet')?.classList.remove('translate-y-full');
  });
}

export function closeMessageOptionsModal() {
  messageState.hideMessageOptions();
  const modal = document.getElementById('msg-options-modal');
  if (!modal) return;

  const sheet = modal.querySelector('.msg-options-sheet');
  if (sheet) {
    sheet.classList.remove('translate-y-0');
    sheet.classList.add('translate-y-full');
    setTimeout(() => {
      modal.classList.add('hidden');
      modal.classList.remove('flex');
    }, 280);
  } else {
    modal.classList.add('hidden');
    modal.classList.remove('flex');
  }
}

// ============================================================================
// CONVERSATION OPTIONS  (⋮ menu)
// ============================================================================

export function openConversationOptionsModal() {
  const modal     = document.getElementById('msg-conv-options-modal');
  const blockBtn  = document.getElementById('msg-conv-opt-block');
  const partner   = messageState.getCurrentPartner();
  if (!modal || !partner) return;

  // Relabel block button based on current state
  if (blockBtn) {
    const isBlocked = messageState.isCurrentPartnerBlockedByMe();
    blockBtn.querySelector('span')?.textContent !== undefined
      ? (blockBtn.querySelector('span').textContent = isBlocked ? `Unblock ${partner.name}` : `Block ${partner.name}`)
      : null;
    blockBtn.dataset.action = isBlocked ? 'unblock-message-user' : 'block-message-user';
  }

  modal.classList.remove('hidden');
  modal.classList.add('flex');
  requestAnimationFrame(() => {
    modal.querySelector('.msg-conv-options-sheet')?.classList.add('translate-y-0');
    modal.querySelector('.msg-conv-options-sheet')?.classList.remove('translate-y-full');
  });
}

export function closeConversationOptionsModal() {
  const modal = document.getElementById('msg-conv-options-modal');
  if (!modal) return;
  const sheet = modal.querySelector('.msg-conv-options-sheet');
  if (sheet) {
    sheet.classList.remove('translate-y-0');
    sheet.classList.add('translate-y-full');
    setTimeout(() => {
      modal.classList.add('hidden');
      modal.classList.remove('flex');
    }, 280);
  } else {
    modal.classList.add('hidden');
    modal.classList.remove('flex');
  }
}

// ============================================================================
// REPORT MESSAGE MODAL
// ============================================================================

export function openReportMessageModal(messageId) {
  const modal = document.getElementById('msg-report-modal');
  if (!modal) return;
  modal.dataset.messageId = messageId;
  // Reset form
  const form = modal.querySelector('form, [data-report-form]');
  form?.reset?.();
  openModal('msg-report-modal');
}

export function closeReportMessageModal() {
  closeModal('msg-report-modal');
}

// ============================================================================
// PARTNER INFO MODAL
// ============================================================================

export function openPartnerInfoModal() {
  const partner   = messageState.getCurrentPartner();
  if (!partner) return;

  const modal     = document.getElementById('msg-partner-info-modal');
  const nameEl    = document.getElementById('msg-info-partner-name');
  const avatarEl  = document.getElementById('msg-info-partner-avatar');
  const statusEl  = document.getElementById('msg-info-partner-status');

  if (!modal) return;

  if (nameEl)   nameEl.textContent   = partner.name;
  if (avatarEl) avatarEl.src         = partner.avatar || '/static/default-avatar.png';
  if (statusEl) statusEl.textContent = messageState.isUserOnline(partner.id) ? 'Online' : 'Offline';

  openModal('msg-partner-info-modal');
}

export function closePartnerInfoModal() {
  closeModal('msg-partner-info-modal');
}

// ============================================================================
// GENERIC CONFIRM MODAL
// ============================================================================

let _onConfirmCallback = null;

/**
 * @param {{ title, message, confirmLabel, confirmClass, onConfirm }} opts
 */
export function openConfirmModal({ title, message, confirmLabel = 'Confirm', confirmClass = 'bg-[var(--accent)] hover:bg-[var(--accent-hover)]', onConfirm }) {
  const modal        = document.getElementById('msg-confirm-modal');
  const titleEl      = document.getElementById('msg-confirm-title');
  const msgEl        = document.getElementById('msg-confirm-message');
  const confirmBtn   = document.getElementById('msg-confirm-ok-btn');

  if (!modal) {
    // Fallback to native confirm
    if (window.confirm(`${title}\n${message}`)) onConfirm?.();
    return;
  }

  if (titleEl)    titleEl.textContent   = title;
  if (msgEl)      msgEl.textContent     = message;
  if (confirmBtn) {
    confirmBtn.textContent = confirmLabel;
    confirmBtn.className   = `px-5 py-2 rounded-xl text-white text-sm font-semibold transition-all
                              active:scale-95 ${confirmClass}`;
  }

  _onConfirmCallback = onConfirm;
  openModal('msg-confirm-modal');
}

export function handleConfirmOk() {
  _onConfirmCallback?.();
  _onConfirmCallback = null;
  closeModal('msg-confirm-modal');
}

export function closeConfirmModal() {
  _onConfirmCallback = null;
  closeModal('msg-confirm-modal');
}

// ============================================================================
// IMAGE VIEWER  (handled by render.js, but keep modal helpers here too)
// ============================================================================

export function closeImageViewer() {
  const modal = document.getElementById('msg-image-viewer-modal');
  modal?.classList.add('hidden');
  modal?.classList.remove('flex');
  document.body.style.overflow = '';
}

// ============================================================================
// PRIVATE
// ============================================================================

function _canDeleteForEveryone(sentAt) {
  if (!sentAt) return false;
  return (Date.now() - new Date(sentAt).getTime()) < 5 * 60 * 1000;
}
