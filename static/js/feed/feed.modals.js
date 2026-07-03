/**
 * ============================================================================
 * FEED MODALS - WITH THREAD CREATION FROM POST
 * Modal-specific logic: comments, reactions, fork, refine, threads
 * FIXED: Thread creation with post author as member
 * ============================================================================
 */

import { feedState } from './feed.state.js';
import { MAX_TAGS } from './feed.constants.js';
import * as feedApi from './feed.api.js';
import {syncPostUI} from './feed.events.js';
import { getLoadingSkeleton, openModal, closeModal } from './feed.utils.js';
import { 
  renderPostComments, 
  appendCommentToUI, 
  renderThreadDetails,
  renderSelectedForkTags,
  renderSelectedPostTags,
  updatePreviousButton,
  renderSelectedThreadTags
} from './feed.render.js';
export function startAuthorOverviewStream(userId) {
  api.get(`/connections/overview/${userId}`).then(response => {
                if (!response.ok) {
                    throw new Error('Failed to connect');
                }
                
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                
                // Hide loader and show AI section
                document.getElementById('loading-section').style.display = 'none';
                document.getElementById('ai-section').style.display = 'block';
                
                function readStream() {
                    reader.read().then(({ done, value }) => {
                        if (done) {
                            // Stream complete
                            document.getElementById('streaming-indicator').style.display = 'none';
                            return;
                        }
                        
                        const chunk = decoder.decode(value, { stream: true });
                        const lines = chunk.split('\n');
                        
                        lines.forEach(line => {
                            if (line.startsWith('data: ')) {
                                try {
                                    const data = JSON.parse(line.slice(6));
                                    
                                    // Handle content streaming
                                    if (data.content) {
                                        fullResponse += data.content;
                                        document.getElementById('ai-text').textContent = fullResponse;
                                    }
                                    
                                    // Handle completion
                                    if (data.type === 'done') {
                                        document.getElementById('streaming-indicator').style.display = 'none';
                                        
                                        if (data.already_connected) {
                                            document.getElementById('success-message').innerHTML = 
                                                '<div class="success-message">✓ You are already connected with this user</div>';
                                        }
                                    }
                                    
                                    // Handle errors
                                    if (data.error) {
                                        showError(data.error);
                                    }
                                    
                                } catch (e) {
                                    console.error('Parse error:', e, line);
                                }
                            }
                        });
                        
                        readStream();
                    }).catch(err => {
                        console.error('Stream error:', err);
                        showError('Connection lost. Please try again.');
                    });
                }
                
                readStream();
            })
            .catch(err => {
                console.error('Fetch error:', err);
                showError('Failed to load post author overview. Please try again.');
            });
        }

/**
 * Open comment modal and load comments
 */
export async function openCommentModal(postId, event) {
  event.stopPropagation();
  const modal = document.getElementById("post-comments-modal");
  if (!modal) {
    console.error("Comments modal not found");
    return;
  }
  feedState.clearCommentModalHistory();
  updatePreviousButton();
  openModal('post-comments-modal');
  
  
  modal.dataset.postId = postId;
  
  const commentsContainer = document.getElementById("comments-container");
  if (!commentsContainer) {
    console.error("Comments container not found");
    return;
  }
  
  commentsContainer.innerHTML = getLoadingSkeleton();
  
  const commentInput = document.getElementById("commentInput");
  if (commentInput) {
    delete commentInput.dataset.parentId;
    commentInput.value = "";
  }
  
  try {
    const comments = await feedApi.getPostComments(postId);
    
    if (!comments || comments.length === 0) {
      commentsContainer.innerHTML = `
        <div class="empty-state" style="text-align: center; padding: 3rem 1rem; color: var(--text-secondary);">
          <svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="margin: 0 auto 1rem; opacity: 0.3;">
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>
          </svg>
          <p>No comments yet. Be the first to comment!</p>
        </div>
      `;
      return;
    }
    
    const commentsHTML = renderPostComments(comments);
    commentsContainer.innerHTML = commentsHTML;
  } catch (error) {
    if (typeof showToast === 'function') {
      showToast('Error loading comments: ' + error.message, 'error');
    }
    commentsContainer.innerHTML = `
      <div class="error-state" style="text-align: center; padding: 3rem 1rem;">
        <p style="color: var(--danger); margin-bottom: 1rem;">Error loading comments: ${error.message}</p>
        <button class="btn btn-primary" onclick="openCommentModal(${postId})">Try again</button>
      </div>
    `;
  }
}

/**
 * Open reply modal
 */
export function openReplyModal(username, commentId, postId) {
  const inputBox = document.getElementById("commentInput");
  
  if (!inputBox) {
    console.error("Comment input not found");
    return;
  }
  
  inputBox.dataset.postId = postId;
  inputBox.dataset.parentId = commentId;
  
  inputBox.value = `@${username} `;
  inputBox.focus();
  
  inputBox.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

/**
 * Post a comment
 */
export async function postComment(event) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }
  
  const inputBox = document.getElementById("commentInput");
  if (!inputBox) {
    return;
  }
  
  const textContent = inputBox.value.trim();
  const modal = document.getElementById("post-comments-modal");
  const postId = modal?.dataset?.postId || inputBox.dataset.postId;
  const parentId = inputBox.dataset.parentId || null;
  
  if (!textContent) {
    if (typeof showToast === 'function') {
      showToast("Comment cannot be empty", "warning");
    }
    return;
  }
  
  if (!postId) {
    return;
  }
  
  const btn = document.getElementById("postCommentBtn");
  if (!btn) {
    return;
  }
  
  const originalText = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Posting...";
  
  try {
    const data = await feedApi.postComment(
      postId, 
      textContent, 
      parentId, 
      feedState.getReplyResources()
    );
    
    if (data.status === "success") {
      inputBox.value = "";
      
      const details = {
        post_id: postId,
        comments_count: data.data?.comment?.comments_count
      };
      
      // Import syncPostUI dynamically to avoid circular dependency
      const { syncPostUI } = await import('./feed.events.js');
      syncPostUI('comment_count', details);

      delete inputBox.dataset.postId;
      delete inputBox.dataset.parentId;
      
      const previewArea = document.getElementById("post-comments-preview-area");
      if (previewArea) previewArea.innerHTML = "";
      
      feedState.clearReplyResources();
      
      const newComment = data.data.comment;
      if (newComment) {
        appendCommentToUI(newComment, parentId);
      }
      
    } else {
      if (typeof showToast === 'function') {
        showToast(data.message || "Failed to post comment", "error");
      }
    }
  } catch (error) {
    console.error("Post comment error:", error);
    if (typeof showToast === 'function') {
      showToast("Error posting comment: " + error.message, 'error');
    }
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}
/**
 * Open fork modal
 */
export function openForkModal(postId) {
  feedState.clearForkTags();
  const modal = document.getElementById("post-fork-modal");
  if (!modal) {
    console.error("Fork modal not found");
    return;
  }
  
  modal.classList.remove('hidden');
  modal.classList.add('active');
  modal.dataset.postId = postId;
  
  const post = document.getElementById(`post-${postId}`);
  if (!post) {
    return;
  }
  
  const postContent = post.querySelector('.post-content');
  const postTitle = post.querySelector('.post-title');
  const postTypeLabel = post.querySelector('.post-type-label');
  const threadBadge = post.querySelector('.thread-badge');
  const postTags = post.querySelectorAll('.post-tags .tag');
  
  const modalContent = modal.querySelector('.post-content');
  const modalTitle = modal.querySelector('.post-title');
  const modalThreadToggle = modal.querySelector('.thread-enabled');
  const modalPostType = modal.querySelector('.post-type-selection');
  
  if (modalContent && postContent) modalContent.value = postContent.textContent.trim();
  if (modalTitle && postTitle) modalTitle.value = postTitle.textContent.trim();
  if (modalThreadToggle) modalThreadToggle.checked = !!threadBadge;
  if (modalPostType && postTypeLabel) modalPostType.value = postTypeLabel.textContent.trim();
  
  const tags = Array.from(postTags).map(tag => tag.textContent.replace('#', '').trim());
  tags.slice(0, MAX_TAGS).forEach(tag => {
    feedState.addForkTag(tag);
  });
  
  renderSelectedForkTags();
}

/**
 * Save forked post
 */
export async function saveForkedPost(event) {
  event.preventDefault();
  
  const modal = document.getElementById("post-fork-modal");
  const saveBtn = modal.querySelector(".save-post");
  const title = modal.querySelector('.post-title').value;
  const content = modal.querySelector('.post-content').value;
  const thread_enabled = modal.querySelector(".thread-enabled").checked;
  const postType = modal.querySelector(".post-type-selection").value;
  
  const formData = {
    "title": title,
    "text_content": content,
    "thread_enabled": thread_enabled,
    "post_type": postType,
    "tags": feedState.getForkTags()
  };
  
  if (saveBtn) saveBtn.disabled = true;
  
  try {
    const response = await feedApi.createForkedPost(formData);
    if (response.status == "success") {
      closeModal("post-fork-modal");
      feedState.clearForkTags();
      return;
    }
    if (typeof showToast === 'function') {
      showToast(response.message, 'error');
    }
  } catch (error) {
    if (typeof showToast === 'function') {
      showToast(error.message, 'error');
    }
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}
/**
 * Add fork tag
 */
export function addForkTag(tag) {
  if (feedState.getForkTags().length >= MAX_TAGS) {
    if (typeof showToast === 'function') {
      showToast(`You can only add up to ${MAX_TAGS} tags`, 'info');
    }
    return;
  }
  
  feedState.addForkTag(tag);
  renderSelectedForkTags();
  
  const input = document.getElementById('fork-tags-input');
  if (input) input.value = '';
  
  const dropdown = document.getElementById('fork-tags-dropdown');
  if (dropdown) dropdown.classList.add('hidden');
}

/**
 * Remove fork tag
 */
export function removeForkTag(tag) {
  feedState.removeForkTag(tag);
  renderSelectedForkTags();
}

export function addPostTag(tag) {
  if (feedState.getPostTags().length >= MAX_TAGS) {
    if (typeof showToast === 'function') {
      showToast(`You can only add up to ${MAX_TAGS} tags`, 'info');
    }
    return;
  }
  
  feedState.addPostTag(tag);
  renderSelectedPostTags();
  
  const input = document.getElementById('tags-input');
  if (input) input.value = '';
  
  const dropdown = document.getElementById('post-tags-dropdown');
  if (dropdown) dropdown.classList.add('hidden');
}

/**
 * Remove post tag
 */
export function removePostTag(tag) {
  feedState.removePostTag(tag);
  renderSelectedPostTags();
}

export function addThreadTag(tag) {
  if (feedState.getThreadTags().length >= MAX_TAGS) {
    if (typeof showToast === 'function') {
      showToast(`You can only add up to ${MAX_TAGS} tags`, 'info');
    }
    return;
  }
  
  feedState.addThreadTag(tag);
  renderSelectedThreadTags();
  
  // Match the actual element IDs in the HTML
  const input = document.getElementById('thread-tag-input');
  if (input) input.value = '';
  
  const dropdown = document.getElementById('thread-tags-dropdown');
  if (dropdown) dropdown.classList.add('hidden');
}

/**
 * Remove thread tag
 */
export function removeThreadTag(tag) {
  feedState.removeThreadTag(tag);
  renderSelectedThreadTags();
}

/**
 * Open thread view modal
 */
export async function viewThread(threadId) {
  const modal = document.getElementById("thread-view-modal");
  const modalBody = modal ? modal.querySelector('#thread-details-content') : null;
  
  if (!modal || !modalBody) {
    console.error("Thread modal not found");
    return;
  }
  
  modal.classList.remove('hidden');
  modalBody.innerHTML = getLoadingSkeleton();
  
  try {
    const data = await feedApi.getThreadDetails(threadId);
    
    if (!data || Object.keys(data).length === 0) {
      modalBody.innerHTML = `
        <div class="empty-state">
          <h1>No data found for this thread</h1>
        </div>`;
      return;
    }
    
    renderThreadDetails(data);
  } catch (error) {
    console.error("View thread error:", error);
    if (typeof showToast === 'function') {
      showToast("Error loading thread: " + error.message, "error");
    }
    modalBody.innerHTML = `
      <div class="error-state">
        <h1>Error loading thread data</h1>
        <button onclick="viewThread(${threadId})">Try again</button>
      </div>`;
  }
}

/**
 * Apply refinement
 */
export async function applyRefinement(postId) {
  const refinement = feedState.getCurrentRefinement();
  if (!refinement) {
    if (typeof showToast === 'function') {
      showToast("No refinement to apply", "error");
    }
    return;
  }
  
  const applyBtn = document.getElementById("apply-btn");
  const originalText = applyBtn ? applyBtn.textContent : "";
  
  try {
    if (applyBtn) {
      applyBtn.disabled = true;
      applyBtn.textContent = "Applying...";
    }
    
    const response = await feedApi.applyPostRefinement(postId, refinement);
    
    if (response && response.status === "success") {
      const postCard = document.querySelector(`[data-post-id="${postId}"]`);
      if (postCard) {
        const titleEl = postCard.querySelector(".post-title");
        const contentEl = postCard.querySelector(".post-content");
        
        if (titleEl) titleEl.textContent = refinement.title;
        if (contentEl) contentEl.textContent = refinement.content;
      }
      
      closeRefineModal();
      feedState.clearRefinement();
    } else {
      if (typeof showToast === 'function') {
        showToast(response?.message || "Failed to apply refinement", "error");
      }
    }
    
  } catch (error) {
    console.error("Apply refinement error:", error);
    if (typeof showToast === 'function') {
      showToast("Error applying refinement: " + error.message, "error");
    }
  } finally {
    if (applyBtn) {
      applyBtn.disabled = false;
      applyBtn.textContent = originalText;
    }
  }
}

/**
 * Close refine modal
 */
export function closeRefineModal() {
  const modal = document.getElementById("post-refine-modal");
  if (modal) {
    modal.classList.add("hidden");
    modal.innerHTML = "";
  }
  feedState.clearRefinement();
}

export async function refineBeforePost(context = 'create', event) {
  let titleEl, contentEl;
  event.stopPropagation();
  
  if (context === 'create') {
    titleEl = document.getElementById('post-title');
    contentEl = document.getElementById('post-content');
  } else if (context === 'fork') {
    titleEl = document.querySelector('#post-fork-modal .post-title');
    contentEl = document.querySelector('#post-fork-modal .post-content');
  }
  
  if (!titleEl || !contentEl) {
    showToast('Cannot find post fields', 'error');
    return;
  }
  
  const title = titleEl.value.trim();
  const content = contentEl.value.trim();
  
  if (!content) {
    showToast('Please enter some content first', 'warning');
    return;
  }
  
  const modal = document.getElementById("inline-post-refine-modal");
  modal.dataset.context = context;
  
  modal.classList.remove("hidden");
  modal.classList.add("active");

  modal.innerHTML = `
    <div class="modal-content refine-modal">
      <div class="modal-header">
        <h3>✨ AI Post Refinement</h3>
        <button class="close-btn" data-modal-id="inline-post-refine-modal" data-action='close-modal'>×</button>
      </div>
      
      <div class="refine-instructions">
        <label for="refinement-instructions">Refinement Instructions (Optional)</label>
        <textarea 
          id="refinement-instructions" 
          placeholder="e.g., Make it more formal, Add more technical details..."
          rows="3"
        ></textarea>
        <button class="btn btn-primary" data-action="start-inline-refinement">
          ✨ Refine Now
        </button>
      </div>
      
      <div class="refine-content">
        <div class="original-content">
          <h4>📝 Original</h4>
          <div class="content-preview">${title}</div>
          <div class="content-preview">${content}</div>
        </div>
        
        <div class="refined-content">
          <h4>✨ Refined</h4>
          <div id="refined-title" class="content-preview">-</div>
          <div id="refined-content" class="content-preview loading">
            Waiting for refinement...
          </div>
        </div>
      </div>
      
      <div class="refine-status" id="refine-status"></div>
      
      <div class="modal-actions hidden" id="refine-actions">
        <button class="btn-secondary" data-modal-id='inline-post-refine-modal' data-action="close-modal">Cancel</button>
        <button class="btn-primary" data-action="apply-inline-refinement">
          Apply Changes
        </button>
      </div>
    </div>
  `;
  
  feedState.setOriginalTitle(title);
  feedState.setOriginalContent(content);
}

export async function startInlineRefinement() {
  const instructions = document.getElementById('refinement-instructions').value;
  const title = feedState.getOriginalTitle();
  const content = feedState.getOriginalContent();
  
  try {
    const response = await fetch('/student/posts/refine-draft', {
      method: 'POST',
      headers: await api.getHeaders(),
      body: JSON.stringify({ title, content, instructions })
    });
    
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let fullResponse = "";
    
    const refinedTitleEl = document.getElementById("refined-title");
    const refinedContentEl = document.getElementById("refined-content");
    const statusEl = document.getElementById("refine-status");
    
    refinedContentEl.classList.remove("loading");
    statusEl.innerHTML = '<div class="loading-indicator"><div class="spinner"></div><span>Refining...</span></div>';
    
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      
      const chunk = decoder.decode(value);
      const lines = chunk.split('\n');
      
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          const data = line.slice(6);
          if (data === '[DONE]') continue;
          
          try {
            const parsed = JSON.parse(data);
            
            if (parsed.content) {
              fullResponse += parsed.content;
              
              const titleMatch = fullResponse.match(/"title"\s*:\s*"([^"]+)"/);
              const contentMatch = fullResponse.match(/"content"\s*:\s*"([^"]+)"/);
              
              if (titleMatch) {
                refinedTitleEl.textContent = titleMatch[1].replace(/\\n/g, '\n');
              }
              if (contentMatch) {
                refinedContentEl.textContent = contentMatch[1].replace(/\\n/g, '\n');
              }
            }
            else if (parsed.type === 'done' && parsed.refined) {
              feedState.setCurrentRefinement(parsed.refined);
              refinedTitleEl.textContent = parsed.refined.title;
              refinedContentEl.textContent = parsed.refined.content;
              
              statusEl.innerHTML = '<div class="success-indicator">✅ Complete!</div>';
              document.getElementById("refine-actions").classList.remove("hidden");
            }
          } catch (e) {
            // Ignore parse errors
          }
        }
      }
    }
    
  } catch (error) {
    console.error("Refinement error:", error);
    showToast("Refinement failed: " + error.message, "error");
  }
}

/**
 * Apply refined content back to form
 */
export function applyInlineRefinement() {
  const refinement = feedState.getCurrentRefinement();
  if (!refinement) {
    showToast("No refinement to apply", "error");
    return;
  }
  
  const modal = document.getElementById("inline-post-refine-modal");
  const context = modal.dataset.context;
  
  let titleEl, contentEl;
  
  if (context === 'create') {
    titleEl = document.getElementById('post-title');
    contentEl = document.getElementById('post-content');
  } else if (context === 'fork') {
    titleEl = document.querySelector('#post-fork-modal .post-title');
    contentEl = document.querySelector('#post-fork-modal .post-content');
  }
  
  if (titleEl) titleEl.value = refinement.title;
  if (contentEl) contentEl.value = refinement.content;
  
  closeModal('inline-post-refine-modal');
}

export async function refinePost(postId) {
  try {
    const modal = document.getElementById("post-refine-modal");
    
    if (!modal) {
      const newModal = document.createElement('div');
      newModal.id = 'post-refine-modal';
      newModal.className = 'modal-overlay hidden';
      newModal.style.cssText = `
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        bottom: 0;
        background: rgba(0,0,0,0.5);
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 1000;
      `;
      document.body.appendChild(newModal);
      console.log('✅ Created refine modal');
    }
    
    const modalToUse = document.getElementById("post-refine-modal");
    modalToUse.dataset.id = postId;
    modalToUse.classList.remove("hidden");
    
    modalToUse.innerHTML = `
      <div class="modal-content refine-modal" style="
        background: var(--bg-primary);
        border-radius: 12px;
        padding: 2rem;
        max-width: 800px;
        width: 90%;
        max-height: 80vh;
        overflow-y: auto;
      ">
        <div class="modal-header" style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem;">
          <h3 style="font-size: 1.5rem; font-weight: 600;">✨ AI Post Refinement</h3>
          <button class="close-btn" 
                  data-action="close-modal" 
                  data-modal-id='post-refine-modal'
                  style="background: none; border: none; font-size: 1.5rem; cursor: pointer; padding: 0.5rem;">×</button>
        </div>
        
        <div class="refine-instructions" style="margin-bottom: 1.5rem;">
          <label for="refinement-instructions" style="display: block; margin-bottom: 0.5rem; font-weight: 500;">Refinement Instructions (Optional)</label>
          <textarea 
            id="refinement-instructions" 
            placeholder="e.g., Make it more formal, Add more technical details, Simplify the language..."
            rows="3"
            style="width: 100%; padding: 0.75rem; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-secondary); resize: vertical;"
          ></textarea>
        </div>
        
        <div class="refine-content" style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1.5rem;">
          <div class="original-content" style="padding: 1rem; background: var(--bg-secondary); border-radius: 8px;">
            <h4 style="margin-bottom: 0.75rem; font-weight: 600;">📝 Original</h4>
            <div id="original-title" class="content-preview" style="margin-bottom: 0.5rem; font-weight: 500;"></div>
            <div id="original-content" class="content-preview" style="line-height: 1.6; color: var(--text-secondary);"></div>
          </div>
          
          <div class="refined-content" style="padding: 1rem; background: var(--bg-secondary); border-radius: 8px;">
            <h4 style="margin-bottom: 0.75rem; font-weight: 600;">✨ Refined</h4>
            <div id="refined-title" class="content-preview loading" style="margin-bottom: 0.5rem; font-weight: 500; color: var(--text-secondary);">Click "Refine" to start...</div>
            <div id="refined-content" class="content-preview loading" style="line-height: 1.6; color: var(--text-secondary);">Waiting for refinement...</div>
          </div>
        </div>
        
        <div class="refine-status" id="refine-status" style="text-align: center; margin-bottom: 1rem;"></div>
        
        <div class="modal-actions" id="refine-actions" style="display: flex; gap: 0.75rem; justify-content: flex-end;">
          <button class="btn-secondary" 
                  data-action="close-modal" 
                  data-modal-id='post-refine-modal'
                  style="padding: 0.75rem 1.5rem; background: var(--bg-secondary); border: none; border-radius: 8px; cursor: pointer;">Cancel</button>
          <button class="refine-button" 
                  data-action="start-refinement"
                  style="padding: 0.75rem 1.5rem; background: var(--primary); color: white; border: none; border-radius: 8px; cursor: pointer; font-weight: 500;">Refine</button>
          <button class="btn-primary hidden" 
                  data-action="apply-refinement" 
                  id="apply-btn"
                  style="padding: 0.75rem 1.5rem; background: var(--success); color: white; border: none; border-radius: 8px; cursor: pointer; font-weight: 500;">
            Apply Changes
          </button>
        </div>
      </div>
    `;
    
    const response = await feedApi.getPostQuickView(postId);
    
    if (response) {
      const post = response;
      const origTitle = document.getElementById("original-title");
      const origContent = document.getElementById("original-content");
      if (origTitle) origTitle.textContent = post.title;
      if (origContent) origContent.textContent = post.content || "[No content]";
    } else {
      if (typeof showToast === 'function') {
        showToast("Failed to load post", "error");
      }
      closeRefineModal();
    }
    
  } catch (error) {
    console.error("Refine post error:", error);
    if (typeof showToast === 'function') {
    }
  }
}

export async function startRefinement(postId) {
  if (!postId) {
    const modal = document.getElementById("post-refine-modal");
    postId = modal?.dataset.id;
    if (!postId) {
      return;
    }
  }
  
  try {
    const instructionsEl = document.getElementById("refinement-instructions");
    const instructions = instructionsEl ? instructionsEl.value : "";
    
    const refinedTitleEl = document.getElementById("refined-title");
    const refinedContentEl = document.getElementById("refined-content");
    const statusEl = document.getElementById("refine-status");
    const refineBtn = document.querySelector('[data-action="start-refinement"]');
    const applyBtn = document.getElementById("apply-btn");
    
    if (refineBtn) refineBtn.disabled = true;
    
    const response = await feedApi.refinePostStream(postId, instructions);
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    
    let fullResponse = "";
    
    if (refinedTitleEl) refinedTitleEl.classList.remove("loading");
    if (refinedContentEl) refinedContentEl.classList.remove("loading");
    if (statusEl) statusEl.innerHTML = '<div class="loading-indicator" style="display: flex; align-items: center; justify-content: center; gap: 0.5rem;"><div class="spinner" style="width: 20px; height: 20px; border: 2px solid var(--primary); border-top: 2px solid transparent; border-radius: 50%; animation: spin 1s linear infinite;"></div><span>Analyzing and refining...</span></div>';
    
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      
      const chunk = decoder.decode(value);
      const lines = chunk.split('\n');
      
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          const data = line.slice(6);
          if (data === '[DONE]') continue;
          
          try {
            const parsed = JSON.parse(data);
            
            if (parsed.type === 'start') {
              if (statusEl) statusEl.innerHTML = '<div class="loading-indicator" style="display: flex; align-items: center; justify-content: center; gap: 0.5rem;"><div class="spinner" style="width: 20px; height: 20px; border: 2px solid var(--primary); border-top: 2px solid transparent; border-radius: 50%; animation: spin 1s linear infinite;"></div><span>Analyzing and refining...</span></div>';
            }
            else if (parsed.content) {
              fullResponse += parsed.content;
              
              const titleMatch = fullResponse.match(/"title"\s*:\s*"([^"]+)"/);
              const contentMatch = fullResponse.match(/"content"\s*:\s*"([^"]+)"/);
              
              if (titleMatch && refinedTitleEl) {
                const title = titleMatch[1]
                  .replace(/\\n/g, '\n')
                  .replace(/\\"/g, '"')
                  .replace(/\\\\/g, '\\');
                refinedTitleEl.textContent = title;
              }
              
              if (contentMatch && refinedContentEl) {
                const content = contentMatch[1]
                  .replace(/\\n/g, '\n')
                  .replace(/\\"/g, '"')
                  .replace(/\\\\/g, '\\');
                refinedContentEl.textContent = content;
              }
            }
            else if (parsed.type === 'done') {
              if (parsed.success && parsed.refined) {
                feedState.setCurrentRefinement(parsed.refined);
                if (refinedTitleEl) refinedTitleEl.textContent = parsed.refined.title;
                if (refinedContentEl) refinedContentEl.textContent = parsed.refined.content;
                
                if (statusEl) statusEl.innerHTML = '<div class="success-indicator" style="color: var(--success); font-weight: 500;">✅ Refinement complete!</div>';
                if (applyBtn) applyBtn.classList.remove("hidden");
              } else {
                if (statusEl) statusEl.innerHTML = '<div class="error-indicator" style="color: var(--danger);">❌ Failed to refine. Please try again.</div>';
              }
            }
            else if (parsed.error) {
              throw new Error(parsed.error);
            }
          } catch (e) {
            if (e.message !== 'Unexpected end of JSON input') {
              console.error('Parse error:', e);
              if (statusEl) statusEl.innerHTML = `<div class="error-indicator" style="color: var(--danger);">❌ ${e.message}</div>`;
            }
          }
        }
      }
    }
    
  } catch (error) {
    console.error("Refinement stream error:", error);
    const statusEl = document.getElementById("refine-status");
    if (statusEl) statusEl.innerHTML = 
      `<div class="error-indicator" style="color: var(--danger);">❌ Error: ${error.message}</div>`;
  } finally {
    const refineBtn = document.querySelector('[data-action="start-refinement"]');
    if (refineBtn) refineBtn.disabled = false;
  }
}


// Make functions globally available for onclick handlers
if (typeof window !== 'undefined') {
  window.openCommentModal = openCommentModal;
  window.openReplyModal = openReplyModal;
  window.postComment = postComment;
  window.openForkModal = openForkModal;
  window.saveForkedPost = saveForkedPost;
  window.addForkTag = addForkTag;
  window.startRefinement = startRefinement;
  window.addThreadTag  = addThreadTag ;

}

      
