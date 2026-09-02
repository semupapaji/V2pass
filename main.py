from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
import asyncio
import sqlite3
import time
import re
import secrets
from datetime import datetime, timedelta
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from contextlib import asynccontextmanager
import uvicorn
import json

# --- CONFIGURATION ---
API_ID = 38520540
API_HASH = '083c83a60885eb385e09cce1376bb0cf'
TARGET_BOT = '@Nick_Bypass_Bot'

# --- DATABASE ---
class Database:
    def __init__(self, db_file='bypass.db'):
        self.db_file = db_file
        self.init_db()
    
    def get_connection(self):
        return sqlite3.connect(self.db_file, check_same_thread=False, timeout=10)
    
    def init_db(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS links (
                id TEXT PRIMARY KEY,
                original_link TEXT,
                bypassed_link TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP,
                expires_at TIMESTAMP,
                request_ip TEXT,
                user_id TEXT DEFAULT 'anonymous'
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS session (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                session_string TEXT,
                is_active BOOLEAN DEFAULT 0,
                phone_number TEXT,
                updated_at TIMESTAMP
            )
        ''')
        cursor.execute('INSERT OR IGNORE INTO session (id) VALUES (1)')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stats (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                total INTEGER DEFAULT 0,
                success INTEGER DEFAULT 0,
                failed INTEGER DEFAULT 0
            )
        ''')
        cursor.execute('INSERT OR IGNORE INTO stats (id) VALUES (1)')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS otp_sessions (
                session_id TEXT PRIMARY KEY,
                phone_number TEXT,
                otp_code TEXT,
                created_at TIMESTAMP,
                expires_at TIMESTAMP,
                is_verified BOOLEAN DEFAULT 0
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def save_link(self, link_id, original_link, request_ip, user_id='anonymous'):
        conn = self.get_connection()
        cursor = conn.cursor()
        expires_at = datetime.now() + timedelta(minutes=10)
        cursor.execute('''
            INSERT INTO links (id, original_link, created_at, expires_at, request_ip, user_id)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (link_id, original_link, datetime.now(), expires_at, request_ip, user_id))
        conn.commit()
        conn.close()
    
    def update_link(self, link_id, bypassed_link, status='success'):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE links SET bypassed_link = ?, status = ?
            WHERE id = ?
        ''', (bypassed_link, status, link_id))
        conn.commit()
        conn.close()
    
    def get_link(self, link_id):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM links WHERE id = ?', (link_id,))
        result = cursor.fetchone()
        conn.close()
        return result
    
    def save_session(self, session_string, phone_number):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE session SET 
                session_string = ?, 
                is_active = 1, 
                phone_number = ?,
                updated_at = ?
            WHERE id = 1
        ''', (session_string, phone_number, datetime.now()))
        conn.commit()
        conn.close()
    
    def get_session(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT session_string, is_active, phone_number FROM session WHERE id = 1')
        result = cursor.fetchone()
        conn.close()
        return result
    
    def clear_session(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('UPDATE session SET session_string = NULL, is_active = 0, phone_number = NULL WHERE id = 1')
        conn.commit()
        conn.close()
    
    def increment_stats(self, status):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('UPDATE stats SET total = total + 1 WHERE id = 1')
        if status == 'success':
            cursor.execute('UPDATE stats SET success = success + 1 WHERE id = 1')
        else:
            cursor.execute('UPDATE stats SET failed = failed + 1 WHERE id = 1')
        conn.commit()
        conn.close()
    
    def get_stats(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT total, success, failed FROM stats WHERE id = 1')
        result = cursor.fetchone()
        conn.close()
        return result if result else (0, 0, 0)
    
    def save_otp_session(self, session_id, phone_number, otp_code):
        conn = self.get_connection()
        cursor = conn.cursor()
        expires_at = datetime.now() + timedelta(minutes=5)
        cursor.execute('''
            INSERT OR REPLACE INTO otp_sessions (session_id, phone_number, otp_code, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (session_id, phone_number, otp_code, datetime.now(), expires_at))
        conn.commit()
        conn.close()
    
    def verify_otp(self, session_id, otp_code):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT phone_number, expires_at, is_verified 
            FROM otp_sessions 
            WHERE session_id = ? AND otp_code = ?
        ''', (session_id, otp_code))
        result = cursor.fetchone()
        
        if not result:
            conn.close()
            return None
        
        phone_number, expires_at, is_verified = result
        
        if is_verified:
            conn.close()
            return None
        
        if datetime.now() > datetime.fromisoformat(expires_at):
            conn.close()
            return None
        
        cursor.execute('''
            UPDATE otp_sessions SET is_verified = 1 WHERE session_id = ?
        ''', (session_id,))
        conn.commit()
        conn.close()
        
        return phone_number
    
    def cleanup_expired(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM links WHERE expires_at < ?', (datetime.now(),))
        cursor.execute('DELETE FROM otp_sessions WHERE expires_at < ?', (datetime.now(),))
        conn.commit()
        conn.close()

# --- FastAPI App ---
app = FastAPI(title="Semy Bypass API")
db = Database()

# --- Userbot Client ---
user_client = None
active_requests = {}
login_clients = {}

# --- Helper Functions ---
def generate_link_id():
    return secrets.token_urlsafe(12)

def filter_bot_name(text):
    if not text:
        return text
    BOT_NAME_PLACEHOLDER = "bypass service"
    for bot_name in ['Nick_Bypass_Bot', 'Nick_Bypass', '_Bypass_Bot']:
        text = text.replace(bot_name, BOT_NAME_PLACEHOLDER)
        text = text.replace(f'@{bot_name}', BOT_NAME_PLACEHOLDER)
    return text

def is_blocked_link(link):
    BLOCKED = ["https://t.me/+I51Yb5mjIsI0ZGJl", "t.me/+I51Yb5mjIsI0ZGJl"]
    return any(b in link for b in BLOCKED)

def format_link(link):
    link = link.strip()
    if link.startswith(('http://', 'https://')):
        return link
    if re.match(r'^[a-zA-Z0-9][a-zA-Z0-9-]*\.[a-zA-Z]{2,}(/.*)?$', link):
        return f'https://{link}'
    return link

def is_valid_url(link):
    return bool(re.match(r'^https?://[a-zA-Z0-9][a-zA-Z0-9-]*\.[a-zA-Z]{2,}(/.*)?$', link))

def compare_links(link1, link2):
    """Compare two links ignoring protocol, www, and trailing slashes"""
    if not link1 or not link2:
        return False
    
    # Clean both links
    link1 = re.sub(r'^https?://', '', link1.lower())
    link2 = re.sub(r'^https?://', '', link2.lower())
    link1 = re.sub(r'^www\.', '', link1)
    link2 = re.sub(r'^www\.', '', link2)
    link1 = link1.rstrip('/')
    link2 = link2.rstrip('/')
    
    return link1 == link2

# --- Userbot Handler with Original Link Matching ---
def setup_userbot_handler(client):
    """Setup the message handler for userbot"""
    
    @client.on(events.NewMessage(from_users=TARGET_BOT))
    async def bypass_response_handler(event):
        response_text = event.text
        response_text = filter_bot_name(response_text)
        
        print(f"📨 Response received: {response_text[:100]}...")
        
        # Extract Original Link from response
        original_match = re.search(r'Original Link :?\s*"?\s*([^\s"\n]+)', response_text, re.IGNORECASE)
        if not original_match:
            print("❌ No original link found in response")
            return
        
        response_original = original_match.group(1).strip()
        print(f"🔗 Response original link: {response_original}")
        
        # Extract Bypass Link from response
        bypass_match = re.search(r'Bypassed Link :?\s*"?\s*([^\s"\n]+)', response_text, re.IGNORECASE)
        if not bypass_match:
            print("❌ No bypass link found in response")
            return
        
        bypass_url = bypass_match.group(1).strip()
        print(f"✅ Bypass link: {bypass_url}")
        
        # Check if bypass link is valid (not expired/deleted message)
        if any(keyword in bypass_url.lower() for keyword in ['not found', 'expired', 'deleted']):
            print("❌ Bypass link is invalid (expired/deleted)")
            return
        
        # Match with pending requests using BOTH original link and timestamp
        matched = False
        for link_id, request_data in list(active_requests.items()):
            if not request_data.get('future') or request_data.get('matched'):
                continue
                
            stored_link = request_data.get('original_link', '')
            
            # Compare links
            if compare_links(stored_link, response_original):
                print(f"✅ Matched! Link ID: {link_id}")
                request_data['matched'] = True
                request_data['future'].set_result({
                    'text': response_text,
                    'bypass_url': bypass_url,
                    'original_link': response_original
                })
                db.update_link(link_id, bypass_url, 'success')
                db.increment_stats('success')
                matched = True
                break
        
        if not matched:
            print(f"❌ No pending request found for link: {response_original}")
            print(f"📋 Active requests: {list(active_requests.keys())}")

# --- API Endpoints ---

@app.get("/")
async def root():
    return {
        "name": "Semy Bypass API",
        "version": "2.0",
        "description": "Multi-user bypass API with proper request matching",
        "endpoints": {
            "/bypass?url=LINK": "Bypass a shortened URL",
            "/admin": "Admin panel - Login with Telegram",
            "/stats": "Get statistics"
        }
    }

@app.get("/bypass")
async def bypass_url(url: str, request: Request):
    """Bypass a shortened URL - Supports multiple concurrent requests"""
    global user_client
    
    if not url:
        raise HTTPException(status_code=400, detail="Missing URL parameter")
    
    # Check userbot
    if not user_client or not user_client.is_connected():
        return {
            "success": False,
            "error": "Userbot not connected. Please login via /admin",
            "status": "userbot_offline"
        }
    
    formatted = format_link(url)
    
    if is_blocked_link(formatted):
        raise HTTPException(status_code=400, detail="This link is blocked")
    
    if not is_valid_url(formatted):
        raise HTTPException(status_code=400, detail="Invalid URL format")
    
    link_id = generate_link_id()
    client_ip = request.client.host if request.client else "unknown"
    
    # Save link with original URL
    db.save_link(link_id, url, client_ip)
    
    # Create unique future for this request
    loop = asyncio.get_event_loop()
    future = loop.create_future()
    
    # Store request with original link for matching
    active_requests[link_id] = {
        'timestamp': time.time(),
        'future': future,
        'matched': False,
        'original_link': formatted,  # Store formatted link for matching
        'raw_link': url              # Store raw link for response
    }
    
    try:
        # Send to target bot
        print(f"📤 Sending to bot: {formatted} (ID: {link_id})")
        await user_client.send_message(TARGET_BOT, formatted)
        
        # Wait for response (13 seconds)
        try:
            response = await asyncio.wait_for(future, timeout=13)
            if response and response.get('bypass_url'):
                bypassed = response['bypass_url']
                if not is_blocked_link(bypassed):
                    return {
                        "success": True,
                        "link_id": link_id,
                        "original_url": url,
                        "bypassed_url": bypassed,
                        "message": "Link bypassed successfully"
                    }
        except asyncio.TimeoutError:
            print(f"⏰ Timeout for link: {formatted} (ID: {link_id})")
        
        # Failed
        db.update_link(link_id, None, 'failed')
        db.increment_stats('failed')
        return {
            "success": False,
            "link_id": link_id,
            "original_url": url,
            "status": "failed",
            "message": "Could not bypass the link or link expired"
        }
        
    except Exception as e:
        print(f"❌ Error: {e}")
        db.update_link(link_id, None, 'error')
        db.increment_stats('failed')
        return {
            "success": False,
            "link_id": link_id,
            "original_url": url,
            "status": "error",
            "message": str(e)
        }
    finally:
        # Cleanup
        if link_id in active_requests:
            del active_requests[link_id]

@app.get("/stats")
async def stats():
    total, success, failed = db.get_stats()
    connected = user_client and user_client.is_connected()
    return {
        "total_requests": total,
        "successful_bypass": success,
        "failed_bypass": failed,
        "success_rate": f"{(success/total*100):.2f}%" if total > 0 else "0%",
        "userbot_status": "online" if connected else "offline",
        "active_requests": len(active_requests)
    }

# --- Admin Panel (HTML) ---
@app.get("/admin", response_class=HTMLResponse)
async def admin_panel():
    """Admin panel with Telegram login"""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Semy Bypass - Admin</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                display: flex;
                justify-content: center;
                align-items: center;
                padding: 20px;
            }
            .container {
                background: white;
                padding: 40px;
                border-radius: 20px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                max-width: 450px;
                width: 100%;
            }
            h1 {
                text-align: center;
                color: #333;
                font-size: 28px;
                margin-bottom: 5px;
            }
            .subtitle {
                text-align: center;
                color: #888;
                margin-bottom: 30px;
                font-size: 14px;
            }
            .status-box {
                background: #f8f9fa;
                padding: 15px;
                border-radius: 12px;
                margin-bottom: 20px;
                text-align: center;
            }
            .status-badge {
                display: inline-block;
                padding: 6px 15px;
                border-radius: 20px;
                font-weight: 600;
                font-size: 14px;
            }
            .online { background: #d4edda; color: #155724; }
            .offline { background: #f8d7da; color: #721c24; }
            .form-group {
                margin-bottom: 20px;
            }
            label {
                display: block;
                margin-bottom: 8px;
                color: #555;
                font-weight: 600;
            }
            input {
                width: 100%;
                padding: 12px 15px;
                border: 2px solid #e0e0e0;
                border-radius: 10px;
                font-size: 16px;
                transition: border-color 0.3s;
            }
            input:focus {
                outline: none;
                border-color: #667eea;
            }
            button {
                width: 100%;
                padding: 14px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                border: none;
                border-radius: 10px;
                font-size: 16px;
                font-weight: 600;
                cursor: pointer;
                transition: transform 0.2s;
            }
            button:hover { transform: scale(1.02); }
            button:active { transform: scale(0.98); }
            button:disabled {
                opacity: 0.6;
                cursor: not-allowed;
            }
            .btn-danger {
                background: #dc3545;
            }
            .btn-danger:hover { background: #c82333; }
            .btn-success {
                background: #28a745;
            }
            .btn-success:hover { background: #218838; }
            .message {
                padding: 12px;
                border-radius: 8px;
                margin-bottom: 15px;
                display: none;
            }
            .success { 
                background: #d4edda; 
                color: #155724; 
                display: block; 
            }
            .error { 
                background: #f8d7da; 
                color: #721c24; 
                display: block; 
            }
            .info {
                background: #d1ecf1;
                color: #0c5460;
                padding: 12px;
                border-radius: 8px;
                margin-bottom: 15px;
                display: none;
            }
            .step {
                background: #f0f0ff;
                padding: 15px;
                border-radius: 10px;
                margin-bottom: 15px;
                border-left: 4px solid #667eea;
            }
            .step-title {
                font-weight: 600;
                color: #333;
                margin-bottom: 5px;
            }
            .step-desc {
                color: #666;
                font-size: 14px;
            }
            #otpSection, #passwordSection {
                display: none;
            }
            .footer {
                text-align: center;
                margin-top: 20px;
                font-size: 13px;
                color: #aaa;
            }
            .footer a {
                color: #667eea;
                text-decoration: none;
            }
            .phone-display {
                background: #e9ecef;
                padding: 8px 12px;
                border-radius: 6px;
                font-family: monospace;
                font-size: 14px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🔐 Admin Panel</h1>
            <p class="subtitle">Login with Telegram</p>
            
            <div id="statusBox" class="status-box">
                <strong>Status:</strong> 
                <span id="statusBadge" class="status-badge offline">● Offline</span>
                <span id="phoneDisplay" style="display:none; margin-left:10px; font-size:13px; color:#666;"></span>
            </div>
            
            <div id="message" class="message"></div>
            <div id="infoBox" class="info"></div>
            
            <!-- Login Section -->
            <div id="loginSection">
                <div class="step">
                    <div class="step-title">📱 Login to Telegram</div>
                    <div class="step-desc">Enter your phone number to receive OTP</div>
                </div>
                
                <div class="form-group">
                    <label for="phoneInput">Phone Number</label>
                    <input type="text" id="phoneInput" placeholder="+91 9876543210" value="+91">
                </div>
                
                <button id="sendOtpBtn" onclick="sendOTP()">📨 Send OTP</button>
            </div>
            
            <!-- OTP Section -->
            <div id="otpSection">
                <div class="step">
                    <div class="step-title">🔑 Enter OTP</div>
                    <div class="step-desc">OTP sent to <span id="otpPhoneDisplay" class="phone-display"></span></div>
                </div>
                
                <div class="form-group">
                    <label for="otpInput">OTP Code</label>
                    <input type="text" id="otpInput" placeholder="Enter 6-digit OTP" maxlength="6">
                </div>
                
                <button id="verifyOtpBtn" onclick="verifyOTP()">✅ Verify OTP</button>
                <br><br>
                <button onclick="resendOTP()" style="background: #6c757d;">🔄 Resend OTP</button>
            </div>
            
            <!-- Password Section (if 2FA enabled) -->
            <div id="passwordSection">
                <div class="step">
                    <div class="step-title">🔐 2FA Password Required</div>
                    <div class="step-desc">Enter your Telegram 2-factor authentication password</div>
                </div>
                
                <div class="form-group">
                    <label for="passwordInput">Password</label>
                    <input type="password" id="passwordInput" placeholder="Enter your 2FA password">
                </div>
                
                <button onclick="verifyPassword()">🔓 Verify Password</button>
            </div>
            
            <!-- Userbot Controls -->
            <div id="userbotControls" style="display:none; margin-top:20px;">
                <button onclick="logoutUserbot()" class="btn-danger">🚪 Logout</button>
            </div>
            
            <div style="margin-top: 20px; padding: 15px; background: #f8f9fa; border-radius: 10px;">
                <p style="font-size: 14px; color: #666;">
                    <strong>🔗 API Endpoint:</strong><br>
                    <code style="background: #e9ecef; padding: 4px 8px; border-radius: 4px;">
                        /bypass?url=LINK
                    </code>
                </p>
                <p style="font-size: 14px; color: #666; margin-top: 10px;">
                    <strong>📊 Stats:</strong> <a href="/stats" target="_blank">View Statistics</a>
                </p>
            </div>
            
            <div class="footer">
                <p>Powered by Semy Bypass API v1.0</p>
            </div>
        </div>
        
        <script>
            let loginSessionId = null;
            let currentPhone = null;
            
            // Check status on load
            window.onload = function() {
                checkStatus();
            };
            
            async function checkStatus() {
                try {
                    const response = await fetch('/admin/status');
                    const data = await response.json();
                    
                    const badge = document.getElementById('statusBadge');
                    const phoneDisplay = document.getElementById('phoneDisplay');
                    
                    if (data.connected) {
                        badge.className = 'status-badge online';
                        badge.textContent = '● Online';
                        if (data.phone) {
                            phoneDisplay.style.display = 'inline';
                            phoneDisplay.textContent = '📱 ' + data.phone;
                        }
                        document.getElementById('loginSection').style.display = 'none';
                        document.getElementById('userbotControls').style.display = 'block';
                    } else {
                        badge.className = 'status-badge offline';
                        badge.textContent = '● Offline';
                        phoneDisplay.style.display = 'none';
                        document.getElementById('loginSection').style.display = 'block';
                        document.getElementById('userbotControls').style.display = 'none';
                        document.getElementById('otpSection').style.display = 'none';
                        document.getElementById('passwordSection').style.display = 'none';
                    }
                } catch (error) {
                    console.error('Error checking status');
                }
            }
            
            async function sendOTP() {
                const phone = document.getElementById('phoneInput').value.trim();
                
                if (!phone || phone.length < 10) {
                    showMessage('Please enter a valid phone number', 'error');
                    return;
                }
                
                const btn = document.getElementById('sendOtpBtn');
                btn.disabled = true;
                btn.textContent = '⏳ Sending...';
                
                hideMessages();
                
                try {
                    const response = await fetch('/admin/login/send-otp', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ phone: phone })
                    });
                    
                    const data = await response.json();
                    
                    if (data.success) {
                        loginSessionId = data.session_id;
                        currentPhone = phone;
                        
                        document.getElementById('otpPhoneDisplay').textContent = phone;
                        document.getElementById('otpSection').style.display = 'block';
                        document.getElementById('otpInput').value = '';
                        document.getElementById('otpInput').focus();
                        
                        showMessage('✅ OTP sent to your Telegram!', 'success');
                    } else {
                        showMessage('❌ ' + data.message, 'error');
                    }
                } catch (error) {
                    showMessage('❌ Network error', 'error');
                } finally {
                    btn.disabled = false;
                    btn.textContent = '📨 Send OTP';
                }
            }
            
            async function verifyOTP() {
                const otp = document.getElementById('otpInput').value.trim();
                
                if (!otp || otp.length < 4) {
                    showMessage('Please enter valid OTP', 'error');
                    return;
                }
                
                if (!loginSessionId) {
                    showMessage('Please request OTP first', 'error');
                    return;
                }
                
                const btn = document.getElementById('verifyOtpBtn');
                btn.disabled = true;
                btn.textContent = '⏳ Verifying...';
                
                hideMessages();
                
                try {
                    const response = await fetch('/admin/login/verify-otp', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ 
                            session_id: loginSessionId, 
                            otp: otp 
                        })
                    });
                    
                    const data = await response.json();
                    
                    if (data.success) {
                        if (data.requires_password) {
                            document.getElementById('passwordSection').style.display = 'block';
                            document.getElementById('otpSection').style.display = 'none';
                            showMessage('🔐 Enter your 2FA password', 'info');
                        } else {
                            showMessage('✅ Login successful!', 'success');
                            setTimeout(() => checkStatus(), 1000);
                        }
                    } else {
                        showMessage('❌ ' + data.message, 'error');
                    }
                } catch (error) {
                    showMessage('❌ Network error', 'error');
                } finally {
                    btn.disabled = false;
                    btn.textContent = '✅ Verify OTP';
                }
            }
            
            async function verifyPassword() {
                const password = document.getElementById('passwordInput').value.trim();
                
                if (!password) {
                    showMessage('Please enter your 2FA password', 'error');
                    return;
                }
                
                if (!loginSessionId) {
                    showMessage('Session expired. Please try again.', 'error');
                    return;
                }
                
                const btn = event.target;
                btn.disabled = true;
                btn.textContent = '⏳ Verifying...';
                
                hideMessages();
                
                try {
                    const response = await fetch('/admin/login/verify-password', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ 
                            session_id: loginSessionId, 
                            password: password 
                        })
                    });
                    
                    const data = await response.json();
                    
                    if (data.success) {
                        showMessage('✅ Login successful!', 'success');
                        setTimeout(() => checkStatus(), 1000);
                    } else {
                        showMessage('❌ ' + data.message, 'error');
                    }
                } catch (error) {
                    showMessage('❌ Network error', 'error');
                } finally {
                    btn.disabled = false;
                    btn.textContent = '🔓 Verify Password';
                }
            }
            
            async function resendOTP() {
                if (!currentPhone) {
                    showMessage('Please enter phone number again', 'error');
                    return;
                }
                
                document.getElementById('otpSection').style.display = 'none';
                document.getElementById('sendOtpBtn').click();
            }
            
            async function logoutUserbot() {
                if (!confirm('Are you sure you want to logout?')) {
                    return;
                }
                
                try {
                    const response = await fetch('/admin/logout', {
                        method: 'POST'
                    });
                    
                    const data = await response.json();
                    
                    if (data.success) {
                        showMessage('✅ Logged out successfully', 'success');
                        setTimeout(() => checkStatus(), 1000);
                    } else {
                        showMessage('❌ ' + data.message, 'error');
                    }
                } catch (error) {
                    showMessage('❌ Network error', 'error');
                }
            }
            
            function showMessage(msg, type) {
                const div = document.getElementById('message');
                const info = document.getElementById('infoBox');
                div.textContent = msg;
                div.className = 'message ' + type;
                div.style.display = 'block';
                info.style.display = 'none';
                setTimeout(() => {
                    div.style.display = 'none';
                }, 6000);
            }
            
            function hideMessages() {
                document.getElementById('message').style.display = 'none';
                document.getElementById('infoBox').style.display = 'none';
            }
            
            // Enter key support
            document.getElementById('phoneInput').addEventListener('keypress', function(e) {
                if (e.key === 'Enter') sendOTP();
            });
            document.getElementById('otpInput').addEventListener('keypress', function(e) {
                if (e.key === 'Enter') verifyOTP();
            });
            document.getElementById('passwordInput').addEventListener('keypress', function(e) {
                if (e.key === 'Enter') verifyPassword();
            });
        </script>
    </body>
    </html>
    """
    return html

# --- Admin Login Endpoints ---

# Store login clients
login_clients = {}

@app.post("/admin/login/send-otp")
async def send_otp(request: Request):
    try:
        data = await request.json()
        phone = data.get('phone')
        
        if not phone:
            return {"success": False, "message": "Phone number required"}
        
        session_id = secrets.token_urlsafe(32)
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        login_clients[session_id] = client
        
        await client.connect()
        
        if not await client.is_user_authorized():
            await client.send_code_request(phone)
            db.save_otp_session(session_id, phone, "sent")
            return {
                "success": True,
                "session_id": session_id,
                "message": "OTP sent to your Telegram"
            }
        else:
            await client.disconnect()
            return {"success": False, "message": "Already logged in"}
            
    except Exception as e:
        print(f"Error sending OTP: {e}")
        return {"success": False, "message": f"Error: {str(e)}"}

@app.post("/admin/login/verify-otp")
async def verify_otp(request: Request):
    try:
        data = await request.json()
        session_id = data.get('session_id')
        otp = data.get('otp')
        
        if not session_id or not otp:
            return {"success": False, "message": "Missing session_id or OTP"}
        
        if session_id not in login_clients:
            return {"success": False, "message": "Invalid session"}
        
        client = login_clients[session_id]
        
        try:
            await client.sign_in(code=otp)
            
            if not await client.is_user_authorized():
                return {
                    "success": True,
                    "requires_password": True,
                    "message": "2FA password required"
                }
            
            session_string = client.session.save()
            me = await client.get_me()
            phone = me.phone
            
            db.save_session(session_string, phone)
            
            global user_client
            user_client = client
            
            # Setup handler
            setup_userbot_handler(user_client)
            
            if session_id in login_clients:
                del login_clients[session_id]
            
            db.verify_otp(session_id, otp)
            
            return {
                "success": True,
                "message": "Login successful"
            }
            
        except Exception as e:
            error_msg = str(e)
            if "password" in error_msg.lower():
                return {
                    "success": True,
                    "requires_password": True,
                    "message": "2FA password required"
                }
            return {"success": False, "message": f"Invalid OTP: {error_msg}"}
            
    except Exception as e:
        print(f"Error verifying OTP: {e}")
        return {"success": False, "message": str(e)}

@app.post("/admin/login/verify-password")
async def verify_password(request: Request):
    try:
        data = await request.json()
        session_id = data.get('session_id')
        password = data.get('password')
        
        if not session_id or not password:
            return {"success": False, "message": "Missing session_id or password"}
        
        if session_id not in login_clients:
            return {"success": False, "message": "Invalid session"}
        
        client = login_clients[session_id]
        
        try:
            await client.sign_in(password=password)
            
            session_string = client.session.save()
            me = await client.get_me()
            phone = me.phone
            
            db.save_session(session_string, phone)
            
            global user_client
            user_client = client
            
            # Setup handler
            setup_userbot_handler(user_client)
            
            if session_id in login_clients:
                del login_clients[session_id]
            
            return {
                "success": True,
                "message": "Login successful"
            }
            
        except Exception as e:
            return {"success": False, "message": f"Invalid password: {str(e)}"}
            
    except Exception as e:
        return {"success": False, "message": str(e)}

@app.post("/admin/logout")
async def logout():
    global user_client
    try:
        if user_client and user_client.is_connected():
            await user_client.disconnect()
        user_client = None
        db.clear_session()
        return {"success": True, "message": "Logged out"}
    except Exception as e:
        return {"success": False, "message": str(e)}

@app.get("/admin/status")
async def admin_status():
    connected = user_client and user_client.is_connected()
    phone = None
    
    if connected:
        try:
            me = await user_client.get_me()
            phone = me.phone
        except:
            pass
    
    return {
        "connected": connected,
        "phone": phone,
        "active_requests": len(active_requests)
    }

# --- Cleanup Loop ---
async def cleanup_loop():
    while True:
        await asyncio.sleep(300)
        db.cleanup_expired()
        print("🧹 Cleaned expired links and sessions")

# --- Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global user_client
    
    print("🚀 Starting Semy Bypass API v2.0...")
    print("📊 Multi-user support with proper request matching")
    
    # Try to restore session
    session_data = db.get_session()
    if session_data and session_data[0]:
        try:
            session_string = session_data[0]
            user_client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
            await user_client.start()
            print(f"✅ Userbot restored for {session_data[2]}")
            setup_userbot_handler(user_client)
        except Exception as e:
            print(f"⚠️ Could not restore session: {e}")
            user_client = None
    
    asyncio.create_task(cleanup_loop())
    
    yield
    
    print("🛑 Shutting down...")
    if user_client and user_client.is_connected():
        await user_client.disconnect()

app.router.lifespan_context = lifespan

# --- Run ---
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)