/**
 * ============================================================================
 * HOMEWORK RENDER FUNCTIONS
 * DOM manipulation and rendering logic
 * Includes cursor-based infinite scroll (IntersectionObserver) for the
 * "My Work" and "Connections" tabs.
 * ============================================================================
 */

import { homeworkState } from './homework.state.js';
import { homeworkAPI } from './homework.api.js';
import {
  renderHomeworkSection,
  renderMyHomeworkList,
  renderMyHomeworkCard,
  renderConnectionsHomeworkList,
  renderConnectionsHomeworkCard,
  renderLoadingState
} from './homework.templates.js';
import { showHomeworkToast } from './homework.utils.js';
import { loadStatsTab, loadDynamicStatsComponents} from './homework.stats.js';

// How many items to fetch per page for both infinite-scroll lists
const PAGE_SIZE = 15;

// Kept module-level so we can disconnect a stale observer before creating
// a new one (tab switches / refreshes re-render the DOM the observer was
// watching, so the old node references go stale).
let myHomeworkObserver = null;
let connectionsObserver = null;

function disconnectObserver(observer) {
  if (observer) observer.disconnect();
}

/**
 * Watches the sentinel element rendered at the bottom of a homework list.
 * When it scrolls into view, `onLoadMore` is invoked to fetch the next page.
 */
function setupInfiniteScroll(prefix, onLoadMore) {
  const sentinel = document.getElementById(`hw-${prefix}-sentinel`);
  if (!sentinel) return null;

  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        onLoadMore();
      }
    });
  }, { root: null, rootMargin: '250px', threshold: 0 });

  observer.observe(sentinel);
  return observer;
}

/**
 * Once a list runs out of pages, swap the sentinel/spinner for a static
 * "end of list" message and stop observing.
 */
function updatePaginationFooter(prefix, hasMore) {
  if (hasMore) return;

  const sentinel = document.getElementById(`hw-${prefix}-sentinel`);
  const loadMoreEl = document.getElementById(`hw-${prefix}-load-more`);

  if (loadMoreEl) {
    loadMoreEl.outerHTML = `<div class="hw-list-end" style="text-align:center;padding:16px 0;color:var(--text-secondary,#9ca3af);font-size:0.82rem;">You're all caught up 🎉</div>`;
  }
  if (sentinel) sentinel.remove();
}

function renderMyHomeworkFromCache() {
  const container = document.getElementById('my-homework-container');
  if (!container) return;

  container.innerHTML = renderMyHomeworkList(
    homeworkState.myAssignments,
    homeworkState.myAssignmentsStats,
    homeworkState.myAssignmentsHasMore
  );

  disconnectObserver(myHomeworkObserver);
  myHomeworkObserver = setupInfiniteScroll('my', loadMoreMyHomework);
}

/**
 * Render connections homework from cached state
 */
function renderConnectionsHomeworkFromCache() {
  const container = document.getElementById('connections-homework-container');
  if (!container) return;

  container.innerHTML = renderConnectionsHomeworkList(
    homeworkState.connectionsHomework,
    homeworkState.connectionsHasMore
  );

  disconnectObserver(connectionsObserver);
  connectionsObserver = setupInfiniteScroll('connections', loadMoreConnectionsHomework);
}

/**
 * Render stats from cached state
 */
function renderStatsFromCache() {
  const container = document.getElementById('stats-container');
  if (!container) return;

  const statsData = homeworkState.getStatsData();
  if (statsData) {
    container.innerHTML = renderStatsDashboard(statsData);
    // Load dynamic components
    loadDynamicStatsComponents();
  }
}


/**
 * Initialize homework section
 */
export async function initializeHomeworkSection() {
  const homeworkSection = document.getElementById('homework');
  
  if (!homeworkSection) {
    console.error('Homework section not found');
    return;
  }

  // Render main structure
  

  // Load initial data
  await loadMyHomework();
}

/**
 * Switch between tabs
 */
export function switchTab(tab) {
  // Update state
  homeworkState.setActiveTab(tab);

  // Update tab buttons
  document.querySelectorAll('.hw-tab').forEach(btn => {
    btn.classList.remove('active');
    if (btn.dataset.tab === tab) {
      btn.classList.add('active');
    }
  });

  // Update tab panels
  document.querySelectorAll('.hw-tab-panel').forEach(panel => {
    panel.classList.remove('active');
    if (panel.dataset.tabPanel === tab) {
      panel.classList.add('active');
    }
  });

  // Load data for active tab (with caching)
  if (tab === 'my-homework') {
    // Only fetch if not already loaded
    if (!homeworkState.isDataLoaded('myHomework')) {
      loadMyHomework();
    } else {
      // Render from cache
      renderMyHomeworkFromCache();
    }
  } else if (tab === 'connections-homework') {
    // Only fetch if not already loaded
    if (!homeworkState.isDataLoaded('connectionsHomework')) {
      loadConnectionsHomework();
    } else {
      // Render from cache
      renderConnectionsHomeworkFromCache();
    }
  } else if (tab === 'stats') {
    // Only fetch if not already loaded
    if (!homeworkState.isDataLoaded('stats')) {
      loadStatsTab();
    } else {
      // Render from cache
      renderStatsFromCache();
    }
  }
}
/**
 * Load my homework — first page only. Resets pagination state.
 */
export async function loadMyHomework() {
  const container = document.getElementById('my-homework-container');
  
  if (!container) return;
  

  try {
    // Show loading
    container.innerHTML = renderLoadingState();
    homeworkState.setLoading('myHomework', true);

    // Fetch first page
    const response = await homeworkAPI.getMyAssignments({
      status: homeworkState.filters.status,
      limit: PAGE_SIZE
    });

    homeworkState.setLoading('myHomework', false);

    if (response.status === 'success') {
      homeworkState.setMyAssignments(response.data, false);
      container.innerHTML = renderMyHomeworkList(
        homeworkState.myAssignments,
        homeworkState.myAssignmentsStats,
        homeworkState.myAssignmentsHasMore
      );

      disconnectObserver(myHomeworkObserver);
      myHomeworkObserver = setupInfiniteScroll('my', loadMoreMyHomework);
    } else {
      throw new Error(response.message || 'Failed to load homework');
    }
  } catch (error) {
    console.error('Error loading my homework:', error);
    homeworkState.setLoading('myHomework', false);
    container.innerHTML = `
      <div class="hw-error-state">
        <p>Failed to load homework. Please try again.</p>
        <button class="hw-btn hw-btn-primary" data-action="reload-my-homework">
          Retry
        </button>
      </div>
    `;
  }
}

/**
 * Fetch and append the next page of "My Work" assignments.
 * Triggered by the IntersectionObserver when the sentinel scrolls into view.
 */
async function loadMoreMyHomework() {
  if (homeworkState.myAssignmentsLoadingMore || !homeworkState.myAssignmentsHasMore) return;

  homeworkState.myAssignmentsLoadingMore = true;
  const loadMoreEl = document.getElementById('hw-my-load-more');
  if (loadMoreEl) loadMoreEl.classList.remove('hidden');

  try {
    const response = await homeworkAPI.getMyAssignments({
      status: homeworkState.filters.status,
      limit: PAGE_SIZE,
      cursor: homeworkState.myAssignmentsCursor
    });

    if (response.status === 'success') {
      const newAssignments = response.data.assignments || [];
      homeworkState.setMyAssignments(response.data, true);

      const list = document.getElementById('hw-my-list');
      if (list) {
        newAssignments.forEach(assignment => {
          list.insertAdjacentHTML('beforeend', renderMyHomeworkCard(assignment));
        });
      }

      updatePaginationFooter('my', homeworkState.myAssignmentsHasMore);

      if (!homeworkState.myAssignmentsHasMore) {
        disconnectObserver(myHomeworkObserver);
        myHomeworkObserver = null;
      }
    } else {
      throw new Error(response.message || 'Failed to load more homework');
    }
  } catch (error) {
    console.error('Error loading more homework:', error);
    showHomeworkToast('Failed to load more homework', 'error');
  } finally {
    homeworkState.myAssignmentsLoadingMore = false;
    const loadMoreElAfter = document.getElementById('hw-my-load-more');
    if (loadMoreElAfter) loadMoreElAfter.classList.add('hidden');
  }
}

/**
 * Load connections homework — first page only. Resets pagination state.
 */
export async function loadConnectionsHomework() {
  const container = document.getElementById('connections-homework-container');
  
  if (!container) return;

  try {
    // Show loading
    container.innerHTML = renderLoadingState();
    homeworkState.setLoading('connectionsHomework', true);

    // Fetch first page
    const response = await homeworkAPI.getConnectionsHomework({ limit: PAGE_SIZE });

    homeworkState.setLoading('connectionsHomework', false);

    if (response.status === 'success') {
      homeworkState.setConnectionsHomework(response.data, false);
      container.innerHTML = renderConnectionsHomeworkList(
        homeworkState.connectionsHomework,
        homeworkState.connectionsHasMore
      );

      disconnectObserver(connectionsObserver);
      connectionsObserver = setupInfiniteScroll('connections', loadMoreConnectionsHomework);
    } else {
      throw new Error(response.message || 'Failed to load connections homework');
    }
  } catch (error) {
    console.error('Error loading connections homework:', error);
    homeworkState.setLoading('connectionsHomework', false);
    container.innerHTML = `
      <div class="hw-error-state">
        <p>Failed to load homework. Please try again.</p>
        <button class="hw-btn hw-btn-primary" data-action="reload-connections-homework">
          Retry
        </button>
      </div>
    `;
  }
}

/**
 * Fetch and append the next page of connections homework.
 * Triggered by the IntersectionObserver when the sentinel scrolls into view.
 */
async function loadMoreConnectionsHomework() {
  if (homeworkState.connectionsLoadingMore || !homeworkState.connectionsHasMore) return;

  homeworkState.connectionsLoadingMore = true;
  const loadMoreEl = document.getElementById('hw-connections-load-more');
  if (loadMoreEl) loadMoreEl.classList.remove('hidden');

  try {
    const response = await homeworkAPI.getConnectionsHomework({
      limit: PAGE_SIZE,
      cursor: homeworkState.connectionsCursor
    });

    if (response.status === 'success') {
      const newItems = response.data.homework || [];
      homeworkState.setConnectionsHomework(response.data, true);

      const list = document.getElementById('hw-connections-list');
      if (list) {
        newItems.forEach(hw => {
          list.insertAdjacentHTML('beforeend', renderConnectionsHomeworkCard(hw));
        });
      }

      updatePaginationFooter('connections', homeworkState.connectionsHasMore);

      if (!homeworkState.connectionsHasMore) {
        disconnectObserver(connectionsObserver);
        connectionsObserver = null;
      }
    } else {
      throw new Error(response.message || 'Failed to load more homework');
    }
  } catch (error) {
    console.error('Error loading more connections homework:', error);
    showHomeworkToast('Failed to load more homework', 'error');
  } finally {
    homeworkState.connectionsLoadingMore = false;
    const loadMoreElAfter = document.getElementById('hw-connections-load-more');
    if (loadMoreElAfter) loadMoreElAfter.classList.add('hidden');
  }
}

/**
 * Refresh current tab
 */
export function refreshCurrentTab() {
  const activeTab = homeworkState.getActiveTab();
  
  // Force refresh by clearing cache
  if (activeTab === 'my-homework') {
    homeworkState.forceRefresh('myHomework');
    loadMyHomework();
  } else if (activeTab === 'connections-homework') {
    homeworkState.forceRefresh('connectionsHomework');
    loadConnectionsHomework();
  } else if (activeTab === 'stats') {
    homeworkState.forceRefresh('stats');
    loadStatsTab();
  }
}

/**
 * Update assignment card UI
 */
export function updateAssignmentCard(assignmentId, updates) {
  const card = document.querySelector(`[data-assignment-id="${assignmentId}"]`);
  
  if (!card) return;

  // Update status badge
  if (updates.status) {
    const statusBadge = card.querySelector('.hw-badge.hw-status');
    if (statusBadge) {
      statusBadge.className = `hw-badge ${getStatusBadgeClass(updates.status)}`;
      statusBadge.textContent = getStatusDisplayText(updates.status);
    }
  }

  // Update shared indicator
  if (updates.is_shared !== undefined) {
    // Refresh the card
    refreshCurrentTab();
  }
}

/**
 * Remove assignment card from UI
 */
export function removeAssignmentCard(assignmentId) {
  const card = document.querySelector(`[data-assignment-id="${assignmentId}"]`);
  
  if (card) {
    card.style.opacity = '0';
    card.style.transform = 'scale(0.95)';
    
    setTimeout(() => {
      card.remove();
      
      // Check if list is empty
      const list = document.querySelector('#my-homework-container .hw-list');
      if (list && list.children.length === 0) {
        refreshCurrentTab();
      }
    }, 300);
  }
}

/**
 * Add assignment card to UI
 */
export function addAssignmentCard(assignment) {
  const list = document.querySelector('#my-homework-container .hw-list');
  
  if (list) {
    const card = renderMyHomeworkCard(assignment);
    list.insertAdjacentHTML('afterbegin', card);
    
    // Animate in
    const newCard = list.firstElementChild;
    newCard.style.opacity = '0';
    newCard.style.transform = 'translateY(-20px)';
    
    setTimeout(() => {
      newCard.style.opacity = '1';
      newCard.style.transform = 'translateY(0)';
    }, 10);
  } else {
    // No list exists, refresh
    refreshCurrentTab();
  }
}
