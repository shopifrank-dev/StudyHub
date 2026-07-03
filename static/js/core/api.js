/**
 * ============================================================================
 * StudyHub API Client with Automatic Token Refresh - FIXED VERSION
 * ============================================================================
 */

const API_BASE_URL = '/student';

/**
 * API Client Class with Token Management
 */
class APIClient {
  constructor(baseURL) {
    this.baseURL = baseURL;
    this.isRefreshing = false;
    this.refreshSubscribers = [];
    this.maxRetries = 1;
  }

  /**
   * Check if current page is a public auth page (no token needed)
   */
  isPublicAuthPage() {
    const path = window.location.pathname;
    const publicPaths = [
      '/student/login',
      '/student/register',
      '/student/reset-password',
      '/student/set-password',
      '/student/verify-email',
      '/student/verify-reset',
      '/student/complete-registration'
    ];
    return publicPaths.some(p => path.startsWith(p));
  }

  /**
   * Get access token from cookie
   */
  getToken() {
    const cookies = document.cookie.split(';');
    for (let cookie of cookies) {
      const [name, value] = cookie.trim().split('=');
      if (name === 'access_token') {
        return value;
      }
    }
    return null;
  }

  /**
   * Check if token is expired or about to expire
   */
  isTokenExpired(token) {
    if (!token) return true;
    
    try {
      const payload = JSON.parse(atob(token.split('.')[1]));
      const now = Math.floor(Date.now() / 1000);
      
      // Check if expired or will expire in next 60 seconds
      return payload.exp < (now + 60);
    } catch (error) {
      console.error('Error checking token expiry:', error);
      return true;
    }
  }

  /**
   * Refresh access token
   */
  async refreshAccessToken() {
    try {
      console.log('🔄 Refreshing access token...');
      
      const response = await fetch(`${this.baseURL}/refresh-token`, {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json'
        }
      });

      if (!response.ok) {
        throw new Error('Token refresh failed');
      }

      const data = await response.json();
      
      if (data.status === 'success') {
        console.log('✅ Token refreshed successfully');
        return true;
      }
      
      throw new Error('Token refresh failed');
    } catch (error) {
      console.error('❌ Token refresh error:', error);
      
      // ✅ FIXED: Only clear auth if NOT on public pages
      if (!this.isPublicAuthPage()) {
        this.clearAuth();
      }
      
      return false;
    }
  }

  /**
   * Clear authentication and redirect
   */
  clearAuth() {
    document.cookie = 'access_token=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;';
    document.cookie = 'refresh_token=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;';
    
    if (typeof showToast === 'function') {
      showToast('Session expired. Please login again.', 'error');
    }
    
    setTimeout(() => {
      window.location.href = '/student/login';
    }, 1500);
  }

  /**
   * Add subscriber for token refresh
   */
  subscribeTokenRefresh(callback) {
    this.refreshSubscribers.push(callback);
  }

  /**
   * Notify all subscribers when token is refreshed
   */
  onTokenRefreshed(token) {
    this.refreshSubscribers.forEach(callback => callback(token));
    this.refreshSubscribers = [];
  }

  /**
   * Get headers with automatic token management
   * ✅ FIXED: Skip token refresh on public pages
   */
  async getHeaders(isJSON = true) {
    const headers = {};
    
    // ✅ FIXED: If on public auth page, don't try to refresh tokens
    if (this.isPublicAuthPage()) {
      if (isJSON) {
        headers['Content-Type'] = 'application/json';
      }
      return headers;
    }
    
    let token = this.getToken();
    
    // Check if token needs refresh
    if (this.isTokenExpired(token)) {
      console.log('⚠️ Token expired or expiring soon, refreshing...');
      
      // Prevent multiple simultaneous refresh requests
      if (this.isRefreshing) {
        return new Promise((resolve) => {
          this.subscribeTokenRefresh((newToken) => {
            headers['Authorization'] = `Bearer ${newToken}`;
            if (isJSON) {
              headers['Content-Type'] = 'application/json';
            }
            resolve(headers);
          });
        });
      }
      
      this.isRefreshing = true;
      
      const refreshed = await this.refreshAccessToken();
      
      this.isRefreshing = false;
      
      if (refreshed) {
        token = this.getToken();
        this.onTokenRefreshed(token);
      } else {
        throw new Error('Authentication failed');
      }
    }
    
    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
    }
    
    if (isJSON) {
      headers['Content-Type'] = 'application/json';
    }
    
    return headers;
  }

  /**
   * Handle API response
   */
  async handleResponse(response) {
    const contentType = response.headers.get('content-type');
    
    if (contentType && contentType.includes('application/json')) {
      const data = await response.json();
      
      if (!response.ok) {
        throw new Error(data.message || 'Request failed');
      }
      
      return data;
    }
    
    if (!response.ok) {
      throw new Error('Request failed');
    }
    
    return response;
  }

  /**
   * GET request
   */
  async get(endpoint, params = {}) {
    const queryString = Object.keys(params).length > 0
      ? '?' + new URLSearchParams(params).toString()
      : '';
    
    const url = `${this.baseURL}${endpoint}${queryString}`;
    
    try {
      const headers = await this.getHeaders();
      
      const response = await fetch(url, {
        method: 'GET',
        headers: headers,
        credentials: 'include'
      });
      
      return await this.handleResponse(response);
    } catch (error) {
      console.error('GET request failed:', error);
      throw error;
    }
  }

  /**
   * POST request - ✅ FIXED: Don't require auth for login/register
   */
  async post(endpoint, data = {}, isFormData = false) {
    const url = `${this.baseURL}${endpoint}`;
    
    try {
      // ✅ FIXED: For auth endpoints, don't try to get auth headers
      const isAuthEndpoint = ['/login', '/register', '/refresh-token', '/verify-email', '/complete-registration', '/reset-password', '/set-password'].some(e => endpoint.includes(e));
      
      const headers = isAuthEndpoint ? {} : await this.getHeaders(!isFormData);
      
      if (!isFormData) {
        headers['Content-Type'] = 'application/json';
      }
      
      const options = {
        method: 'POST',
        credentials: 'include',
        headers: headers
      };
      
      if (isFormData) {
        delete options.headers['Content-Type'];
        options.body = data;
      } else {
        options.body = JSON.stringify(data);
      }
      
      const response = await fetch(url, options);
      return await this.handleResponse(response);
    } catch (error) {
      console.error('POST request failed:', error);
      throw error;
    }
  }
  async put(endpoint, data = {}, isFormData = false) {
    const url = `${this.baseURL}${endpoint}`;
    
    try {
      // ✅ FIXED: For auth endpoints, don't try to get auth headers
      const isAuthEndpoint = ['/login', '/register', '/refresh-token', '/verify-email', '/complete-registration', '/reset-password', '/set-password'].some(e => endpoint.includes(e));
      
      const headers = isAuthEndpoint ? {} : await this.getHeaders(!isFormData);
      
      if (!isFormData) {
        headers['Content-Type'] = 'application/json';
      }
      
      const options = {
        method: 'PUT',
        credentials: 'include',
        headers: headers
      };
      
      if (isFormData) {
        delete options.headers['Content-Type'];
        options.body = data;
      } else {
        options.body = JSON.stringify(data);
      }
      
      const response = await fetch(url, options);
      return await this.handleResponse(response);
    } catch (error) {
      console.error('PUT request failed:', error);
      throw error;
    }
  }

  /**
   * PATCH request
   */
  async patch(endpoint, data = {}) {
    const url = `${this.baseURL}${endpoint}`;
    
    try {
      const headers = await this.getHeaders();
      
      const response = await fetch(url, {
        method: 'PATCH',
        headers: headers,
        credentials: 'include',
        body: JSON.stringify(data)
      });
      
      return await this.handleResponse(response);
    } catch (error) {
      console.error('PATCH request failed:', error);
      throw error;
    }
  }

  /**
   * DELETE request
   */
  async delete(endpoint) {
    const url = `${this.baseURL}${endpoint}`;
    
    try {
      const headers = await this.getHeaders();
      
      const response = await fetch(url, {
        method: 'DELETE',
        headers: headers,
        credentials: 'include'
      });
      
      return await this.handleResponse(response);
    } catch (error) {
      console.error('DELETE request failed:', error);
      throw error;
    }
  }

  /**
   * Upload file
   */
  async uploadFile(endpoint, fileInput, additionalData = {}) {
    const formData = new FormData();
    
    if (fileInput.files && fileInput.files[0]) {
      formData.append('file', fileInput.files[0]);
    }
    
    for (const [key, value] of Object.entries(additionalData)) {
      formData.append(key, value);
    }
    
    return await this.post(endpoint, formData, true);
  }

  /**
   * Verify authentication status - ✅ FIXED: Don't call on public pages
   */
  async verifyAuth() {
    try {
      // ✅ FIXED: If on public auth page, don't verify
      if (this.isPublicAuthPage()) {
        console.log('ℹ️ On public auth page, skipping auth verification');
        return null;
      }
      
      const token = this.getToken();
      
      if (!token) {
        console.log('❌ No token found');
        return null;
      }
      
      // Check if token is expired
      if (this.isTokenExpired(token)) {
        console.log('⚠️ Token expired, attempting refresh...');
        const refreshed = await this.refreshAccessToken();
        
        if (!refreshed) {
          return null;
        }
      }
      
      const headers = {
        'Authorization': `Bearer ${this.getToken()}`
      };
      
      const response = await fetch(`${this.baseURL}/verify-auth`, {
        method: 'GET',
        credentials: 'include',
        headers: headers
      });

      const data = await response.json();
      
      if (data.authenticated) {
        return data.data.user;
      }
      
      return null;
    } catch (error) {
      console.error('Auth verification failed:', error);
      return null;
    }
  }
}

// Create global API instance
const api = new APIClient(API_BASE_URL);

/**
 * ============================================================================
 * AUTHENTICATION HELPERS
 * ============================================================================
 */

function isAuthenticated() {
  const token = api.getToken();
  return token !== null && !api.isTokenExpired(token);
}

function getCurrentUser() {
  const token = api.getToken();
  if (!token) return null;
  
  try {
    const payload = JSON.parse(atob(token.split('.')[1]));
    return {
      id: payload.user_id,
      email: payload.email,
      username: payload.username,
      name: payload.name,
      role: payload.role
    };
  } catch (error) {
    console.error('Error decoding token:', error);
    return null;
  }
}

async function requireAuth() {
  const user = await api.verifyAuth();
  
  if (!user) {
    window.location.href = '/student/login';
    return false;
  }
  
  return true;
}

async function logout() {
  try {
    await api.post('/logout');
    window.location.href = '/student/login';
  } catch (error) {
    console.error('Logout failed:', error);
    api.clearAuth();
  }
}

/**
 * ============================================================================
 * UI HELPER FUNCTIONS
 * ============================================================================
 */

function setButtonLoading(button, isLoading, loadingText = 'Loading...') {
  if (!button) return;
  
  if (isLoading) {
    button.dataset.originalText = button.innerHTML;
    button.innerHTML = `<span class="spinner"></span> ${loadingText}`;
    button.disabled = true;
  } else {
    button.innerHTML = button.dataset.originalText || loadingText;
    button.disabled = false;
  }
}

function debounce(func, wait) {
  let timeout;
  return function executedFunction(...args) {
    const later = () => {
      clearTimeout(timeout);
      func(...args);
    };
    clearTimeout(timeout);
    timeout = setTimeout(later, wait);
  };
}

function isValidEmail(email) {
  const regex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  return regex.test(email);
}

function isValidUsername(username) {
  const regex = /^[a-z0-9]{3,20}$/;
  return regex.test(username);
}

function showToast(message, type = "info", duration = 6000) {
  let container = document.getElementById("toast-container");
  if (!container) {
    container = document.createElement("div");
    container.id = "toast-container";
    container.style.cssText = "position:fixed;top:20px;right:20px;z-index:var(--z-tooltip, 9999);display:flex;flex-direction:column;gap:10px;";
    document.body.appendChild(container);
  }

  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;

  // Status colors map to your existing CSS variables; default/info uses accent
  const accentColors = {
    success: "var(--success)",
    error: "var(--danger)",
    warning: "var(--warning)",
    info: "var(--accent)"
  };
  const stripeColor = accentColors[type] || accentColors.info;

toast.style.cssText = `
  position: fixed;
  bottom: 24px;
  left: 50%;
  transform: translateX(-50%);
  display:flex;
  align-items:center;
  gap:10px;
  padding:12px 20px;
  border-radius:var(--radius-md);
  font-family:sans-serif;
  font-size:14px;
  color:var(--text-primary);
  background:var(--bg-card);
  border:1px solid var(--border-light);
  border-left:3px solid ${stripeColor};
  box-shadow:var(--shadow-lg);
  opacity:0;
  transition:opacity var(--transition-base), transform var(--transition-base);
  z-index: 9999;
  max-width: 90%;
  white-space: nowrap;
`;

  toast.textContent = message;
  container.appendChild(toast);

  // Fade/slide in
  requestAnimationFrame(() => {
    toast.style.opacity = "1";
    toast.style.transform = "translateX(0)";
  });

  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateX(8px)";
    setTimeout(() => toast.remove(), 200);
  }, duration);
}

window.addEventListener('unhandledrejection', (event) => {
  console.error('Unhandled promise rejection:', event.reason);
  if (typeof showToast === 'function') {
    showToast('An error occurred. Please try again.', 'error');
  }
});