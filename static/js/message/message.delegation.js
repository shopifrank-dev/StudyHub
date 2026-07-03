/**
 * Message System Delegation Handlers — PRODUCTION
 * Import MessageHandlers into app.unified.js and spread into UNIFIED_ACTIONS.
 *
 * NOTE: 'open-message-options' is intentionally ABSENT here.
 *       Message options are opened via long-press / right-click (message.longpress.js),
 *       NOT on click — matching WhatsApp / Telegram UX.
 */

import * as events from './message.events.js';
import * as modals from './message.modals.js';
import * as render from './message.render.js';

const toast = (msg, type) => { const fn = window.showToast || globalThis.showToast; fn?.(msg, type); };

export const MessageHandlers = {

  // ── Navigation ─────────────────────────────────────────────────────────────

  'open-conversation': (target) => {
    events.handleOpenConversation(target);
    const headerBar = document.getElementById('header-bar');
    if (headerBar) headerBar.classList.add('hidden');
  },
  'audio-call': () => {
    toast('Audio call feature coming soon', 'info');
  },
  'video-call': () => {
    toast('Video call feature coming soon', 'info');
  },

  'back-to-conversations': () => {
    events.handleCloseConversation();
  },

  'close-conversation': () => {
    events.handleCloseConversation();
  },

  // ── Messaging ─────────────────────────────────────────────────────────────

  'send-message': (target, event) => {
    events.handleSendMessage();
  },

  'handle-message-input': (target, event) => {
    events.handleMessageInput(event);
  },

  'attach-file': () => {
    events.handleAttachFile();
  },

  'remove-pending-attachment': (target) => {
    events.handleRemovePendingAttachment(target);
  },

  'retry-message': (target) => {
    events.handleRetryMessage(target);
  },

  // ── Message options (from the bottom sheet, after long-press opens it) ──

  'delete-message-for-me': () => {
    events.handleDeleteMessageForMe();
  },

  'delete-message-for-everyone': () => {
    events.handleDeleteMessageForEveryone();
  },

  'copy-message': () => {
    events.handleCopyMessage();
  },

  'report-message-open': () => {
    events.handleReportMessage();
  },

  'submit-report': (target) => {
    events.handleSubmitReport(target);
  },

  'close-message-options': () => {
    modals.closeMessageOptionsModal();
  },

  // ── Reactions ──────────────────────────────────────────────────────────────

  'react-to-message': (target, event) => {
    event.stopPropagation();
    events.handleReactToMessage(target);
  },

  'select-message-reaction': (target) => {
    events.handleSelectReaction(target);
  },

  'close-reaction-picker': () => {
    modals.closeReactionPicker();
  },

  // ── Conversation options ───────────────────────────────────────────────────

  'open-conversation-options': () => {
    events.handleOpenConversationOptions();
  },

  'close-conversation-options': () => {
    modals.closeConversationOptionsModal();
  },

  'clear-chat': () => {
    events.handleClearChat();
  },

  

  'unblock-message-user': () => {
    events.handleUnblockUser();
  },
  'block-message-user': () => {
    events.handleBlockUser();
  },

  // ── Partner info ───────────────────────────────────────────────────────────

  'open-partner-info': () => {
    events.handleOpenPartnerInfo();
  },

  'close-partner-info': () => {
    modals.closePartnerInfoModal();
  },

  // ── Image viewer ───────────────────────────────────────────────────────────

  'view-image': (target, event) => {
    event.preventDefault();
    events.handleViewImage(target);
  },

  'close-image-viewer': () => {
    render.closeImageViewer();
  },

  // ── Confirm modal ──────────────────────────────────────────────────────────

  'confirm-ok': () => {
    modals.handleConfirmOk();
  },

  'confirm-cancel': () => {
    modals.closeConfirmModal();
  },

  // ── Report modal ───────────────────────────────────────────────────────────

  'close-report-modal': () => {
    modals.closeReportMessageModal();
  },
};
