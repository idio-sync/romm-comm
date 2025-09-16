import discord
from discord.ext import commands
import logging
import aiohttp
from aiohttp import web, web_request
import aiosqlite
import json
from datetime import datetime, timezone
from pathlib import Path
import os
from typing import Optional, Dict, List
import asyncio
import secrets
import hashlib
import base64

logger = logging.getLogger(__name__)

class WebDashboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.app = None
        self.runner = None
        self.site = None
        
        # Configuration
        self.web_port = int(os.getenv('WEB_DASHBOARD_PORT', 8080))
        self.web_host = os.getenv('WEB_DASHBOARD_HOST', '0.0.0.0')
        self.dashboard_token = os.getenv('DASHBOARD_PASSWORD', self.generate_token())
        self.web_enabled = os.getenv('WEB_DASHBOARD_ENABLED', 'true').lower() == 'true'
        
        # Database path (same as requests cog)
        self.data_dir = Path('data')
        self.db_path = self.data_dir / 'requests.db'
        
        # Start the web server
        if self.web_enabled:
            bot.loop.create_task(self.start_web_server())
        
    def generate_token(self) -> str:
        """Generate a random token for dashboard access"""
        return secrets.token_urlsafe(32)
    
    async def start_web_server(self):
        """Start the aiohttp web server"""
        try:
            await self.bot.wait_until_ready()
            
            # Create the web application
            self.app = web.Application()
            
            # Add routes
            self.app.router.add_get('/', self.dashboard_home)
            self.app.router.add_get('/requests', self.requests_page)
            self.app.router.add_get('/api/requests', self.api_get_requests)
            self.app.router.add_post('/api/requests/{request_id}/fulfill', self.api_fulfill_request)
            self.app.router.add_post('/api/requests/{request_id}/reject', self.api_reject_request)
            self.app.router.add_delete('/api/requests/{request_id}/delete', self.api_delete_request)
            self.app.router.add_post('/api/requests/{request_id}/note', self.api_add_note)
            self.app.router.add_get('/static/{filename}', self.serve_static)
            
            # Create runner and start server
            self.runner = web.AppRunner(self.app)
            await self.runner.setup()
            
            self.site = web.TCPSite(self.runner, self.web_host, self.web_port)
            await self.site.start()
            
            logger.info(f"Requests dashboard started on http://{self.web_host}:{self.web_port}")
            
        except Exception as e:
            logger.error(f"Failed to start web server: {e}")
    
    async def stop_web_server(self):
        """Stop the web server"""
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
    
    def check_auth(self, request: web_request.Request) -> bool:
        """Check if request has valid authentication"""
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            # Check for token in query params as fallback
            token = request.query.get('token')
            return token == self.dashboard_token
        
        if auth_header.startswith('Bearer '):
            token = auth_header[7:]
            return token == self.dashboard_token
        
        return False
    
    async def dashboard_home(self, request: web_request.Request):
        """Serve the main dashboard page"""
        html = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>ROM Requests Dashboard</title>
            <style>
                * {{
                    margin: 0;
                    padding: 0;
                    box-sizing: border-box;
                }}
                
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    min-height: 100vh;
                    padding: 20px;
                }}
                
                .container {{
                    max-width: 1200px;
                    margin: 0 auto;
                    background: white;
                    border-radius: 15px;
                    box-shadow: 0 10px 30px rgba(0,0,0,0.2);
                    overflow: hidden;
                }}
                
                .header {{
                    background: linear-gradient(45deg, #2c3e50, #3498db);
                    color: white;
                    padding: 30px;
                    text-align: center;
                }}
                
                .header h1 {{
                    font-size: 2.5em;
                    margin-bottom: 10px;
                }}
                
                .header p {{
                    opacity: 0.9;
                    font-size: 1.1em;
                }}
                
                .auth-section {{
                    padding: 40px;
                    text-align: center;
                }}
                
                .auth-form {{
                    max-width: 400px;
                    margin: 0 auto;
                }}
                
                .form-group {{
                    margin-bottom: 20px;
                    text-align: left;
                }}
                
                label {{
                    display: block;
                    margin-bottom: 8px;
                    font-weight: 600;
                    color: #2c3e50;
                }}
                
                input[type="password"] {{
                    width: 100%;
                    padding: 12px 15px;
                    border: 2px solid #ddd;
                    border-radius: 8px;
                    font-size: 16px;
                    transition: border-color 0.3s;
                }}
                
                input[type="password"]:focus {{
                    outline: none;
                    border-color: #3498db;
                }}
                
                .btn {{
                    background: linear-gradient(45deg, #3498db, #2980b9);
                    color: white;
                    padding: 12px 30px;
                    border: none;
                    border-radius: 8px;
                    cursor: pointer;
                    font-size: 16px;
                    font-weight: 600;
                    transition: transform 0.2s;
                    width: 100%;
                }}
                
                .btn:hover {{
                    transform: translateY(-2px);
                }}
                
                .error {{
                    color: #e74c3c;
                    background: #fdf2f2;
                    padding: 10px;
                    border-radius: 5px;
                    margin-top: 15px;
                    display: none;
                }}
                
                .footer {{
                    text-align: center;
                    padding: 20px;
                    color: #7f8c8d;
                    border-top: 1px solid #ecf0f1;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>🎮 ROM Requests Dashboard</h1>
                    <p>Manage and track ROM requests for your Romm server</p>
                </div>
                
                <div class="auth-section">
                    <div class="auth-form">
                        <h2>Access Dashboard</h2>
                        <form onsubmit="authenticate(event)">
                            <div class="form-group">
                                <label for="token">Password:</label>
                                <input type="password" id="token" placeholder="Enter your password" required>
                            </div>
                            <button type="submit" class="btn">Access Dashboard</button>
                        </form>
                        <div class="error" id="error-msg"></div>
                    </div>
                </div>
                
                <div class="footer">
                    <p> • Powered by <a href="https://github.com/idio-sync/romm-comm">Romm-Comm</a> • </p>
                </div>
            </div>
            
            <script>
                async function authenticate(event) {{
                    event.preventDefault();
                    const token = document.getElementById('token').value;
                    const errorDiv = document.getElementById('error-msg');
                    
                    try {{
                        const response = await fetch('/api/requests', {{
                            headers: {{
                                'Authorization': `Bearer ${{token}}`
                            }}
                        }});
                        
                        if (response.ok) {{
                            // Store token and redirect
                            localStorage.setItem('dashboardToken', token);
                            window.location.href = '/requests';
                        }} else {{
                            errorDiv.textContent = 'Invalid token. Please try again.';
                            errorDiv.style.display = 'block';
                        }}
                    }} catch (error) {{
                        errorDiv.textContent = 'Connection error. Please try again.';
                        errorDiv.style.display = 'block';
                    }}
                }}
            </script>
        </body>
        </html>
        """
        return web.Response(text=html, content_type='text/html')
    
    async def requests_page(self, request: web_request.Request):
        """Serve the requests management page"""
        html = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Requests Dashboard</title>
            <style>
                * {{
                    margin: 0;
                    padding: 0;
                    box-sizing: border-box;
                }}

                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    background: #f8f9fa;
                    min-height: 100vh;
                }}

                .header {{
                    background: linear-gradient(45deg, #2c3e50, #3498db);
                    color: white;
                    padding: 20px 0;
                    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                }}

                .header-content {{
                    max-width: 1200px;
                    margin: 0 auto;
                    padding: 0 20px;
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                }}

                .header h1 {{
                    font-size: 1.8em;
                }}

                .stats {{
                    display: flex;
                    gap: 20px;
                }}

                .stat-item {{
                    text-align: center;
                }}

                .stat-number {{
                    font-size: 1.5em;
                    font-weight: bold;
                }}

                .stat-label {{
                    font-size: 0.9em;
                    opacity: 0.9;
                }}

                .container {{
                    max-width: 1200px;
                    margin: 0 auto;
                    padding: 20px;
                }}

                .controls {{
                    background: white;
                    padding: 20px;
                    border-radius: 10px;
                    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                    margin-bottom: 20px;
                    display: flex;
                    gap: 15px;
                    align-items: center;
                    flex-wrap: wrap;
                }}

                .filter-group {{
                    display: flex;
                    align-items: center;
                    gap: 10px;
                }}

                .filter-group label {{
                    font-weight: 600;
                    color: #2c3e50;
                }}

                select, input {{
                    padding: 8px 12px;
                    border: 2px solid #ddd;
                    border-radius: 5px;
                    font-size: 14px;
                }}

                select:focus, input:focus {{
                    outline: none;
                    border-color: #3498db;
                }}

                .requests-grid {{
                    display: grid;
                    gap: 15px;
                    grid-template-columns: repeat(2, 1fr);
                }}

                /* Enhanced request card styles - more compact */
                .request-card {{
                    background: white;
                    border-radius: 12px;
                    box-shadow: 0 3px 12px rgba(0,0,0,0.1);
                    overflow: hidden;
                    transition: all 0.3s ease;
                    border: 1px solid #e9ecef;
                    display: flex;
                    flex-direction: column;
                    height: 100%;
                }}

                .request-card:hover {{
                    transform: translateY(-3px);
                    box-shadow: 0 6px 20px rgba(0,0,0,0.15);
                }}

                .request-card.has-igdb {{
                    border-left: 4px solid #9b59b6;
                }}

                .request-card.no-igdb {{
                    border-left: 4px solid #95a5a6;
                }}

                .request-header {{
                    padding: 15px 20px 12px 20px;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    color: white;
                    position: relative;
                    display: flex;
                    justify-content: space-between;
                    align-items: flex-start;
                    flex-shrink: 0;
                }}

                .request-header-left {{
                    flex: 1;
                    min-width: 0;
                }}

                .request-header-right {{
                    flex-shrink: 0;
                    margin-left: 15px;
                }}

                .request-title {{
                    font-size: 1.1em;
                    font-weight: bold;
                    margin-bottom: 6px;
                    line-height: 1.2;
                    word-wrap: break-word;
                }}

                .request-meta {{
                    font-size: 0.85em;
                    opacity: 0.9;
                    font-weight: 500;
                }}
                
                .request-content-wrapper {{
                    display: flex;
                    flex-direction: column;
                    flex: 1; 
                    min-height: 0; 
                }}
                
                .request-main-content {{
                    display: flex;
                    flex: 1;
                    min-height: 200px;
                }}

                .request-cover {{
                    flex: 0 0 270px;
                    background: #f8f9fa;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    border-right: 1px solid #e9ecef;
                    padding: 12px;
                }}

                .request-cover img {{
                    max-width: 100%;
                    max-height: 300px;
                    object-fit: cover;
                    border-radius: 8px;
                    box-shadow: 0 4px 12px rgba(0,0,0,0.2);
                    transition: transform 0.3s ease;
                    opacity: 0;
                    animation: fadeIn 0.5s ease-in-out forwards;
                }}

                .request-metadata {{
                    flex: 250px;
                    padding: 15px;
                    display: flex;
                    flex-direction: column;
                    gap: 10px;
                    min-width: 0;
                    overflow: hidden; 
                    justify-content: flex-start;
                }}

                .request-summary {{
                    padding: 0 20px 15px 20px;
                    border-top: 1px solid #e9ecef;
                    flex-shrink: 0;
                }}

                .request-field {{
                    display: flex;
                    flex-direction: column;
                    gap: 3px;
                }}

                .field-label {{
                    font-weight: 600;
                    color: #2c3e50;
                    font-size: 0.8em;
                    text-transform: uppercase;
                    letter-spacing: 0.5px;
                }}

                .field-value {{
                    color: #34495e;
                    line-height: 1.3;
                    font-size: 0.9em;
                    word-wrap: break-word;
                }}

                .summary-text {{
                    font-style: italic;
                    color: #5a6c7d;
                    border-left: 3px solid #3498db;
                    padding-left: 10px;
                    margin-left: 5px;
                    margin-top: 10px;
                    font-size: 0.9em;
                    line-height: 1.4;
                }}

                .status-badge {{
                    display: inline-block;
                    padding: 4px 10px;
                    border-radius: 20px;
                    font-size: 0.7em;
                    font-weight: 600;
                    text-transform: uppercase;
                    letter-spacing: 0.5px;
                    white-space: nowrap;
                }}

                .status-pending {{
                    background: linear-gradient(45deg, #f39c12, #e67e22);
                    color: white;
                }}

                .status-fulfilled {{
                    background: linear-gradient(45deg, #27ae60, #2ecc71);
                    color: white;
                }}

                .status-rejected {{
                    background: linear-gradient(45deg, #e74c3c, #c0392b);
                    color: white;
                }}

                .status-cancelled {{
                    background: linear-gradient(45deg, #95a5a6, #7f8c8d);
                    color: white;
                }}

                .igdb-link {{
                    color: #9b59b6;
                    text-decoration: none;
                    font-weight: 600;
                    padding: 4px 10px;
                    border: 2px solid #9b59b6;
                    border-radius: 15px;
                    display: inline-block;
                    transition: all 0.3s ease;
                    font-size: 0.8em;
                }}

                .igdb-link:hover {{
                    background: #9b59b6;
                    color: white;
                    transform: translateY(-1px);
                }}

                .request-actions {{
                    padding: 12px 15px;
                    background: #f8f9fa;
                    border-top: 1px solid #e9ecef;
                    display: flex;
                    gap: 8px;
                    flex-wrap: wrap;
                    flex-shrink: 0;
                    margin-top: auto;
                }}

                .btn {{
                    padding: 6px 12px;
                    border: none;
                    border-radius: 6px;
                    cursor: pointer;
                    font-size: 12px;
                    font-weight: 600;
                    transition: all 0.3s ease;
                    text-transform: uppercase;
                    letter-spacing: 0.3px;
                    flex: 1;
                    min-width: 60px;
                }}

                .btn-success {{
                    background: linear-gradient(45deg, #27ae60, #2ecc71);
                    color: white;
                }}

                .btn-danger {{
                    background: linear-gradient(45deg, #e74c3c, #c0392b);
                    color: white;
                }}

                .btn-warning {{
                    background: linear-gradient(45deg, #f39c12, #e67e22);
                    color: white;
                }}

                .btn-secondary {{
                    background: linear-gradient(45deg, #95a5a6, #7f8c8d);
                    color: white;
                }}

                .btn:hover:not(:disabled) {{
                    transform: translateY(-2px);
                    box-shadow: 0 4px 12px rgba(0,0,0,0.2);
                }}

                .btn:disabled {{
                    opacity: 0.5;
                    cursor: not-allowed;
                    transform: none;
                }}

                .loading {{
                    text-align: center;
                    padding: 40px;
                    color: #6c757d;
                }}

                .modal {{
                    display: none;
                    position: fixed;
                    top: 0;
                    left: 0;
                    width: 100%;
                    height: 100%;
                    background: rgba(0,0,0,0.5);
                    z-index: 1000;
                }}

                .modal-content {{
                    position: absolute;
                    top: 50%;
                    left: 50%;
                    transform: translate(-50%, -50%);
                    background: white;
                    padding: 20px;
                    border-radius: 10px;
                    max-width: 500px;
                    width: 90%;
                }}

                .modal-header {{
                    margin-bottom: 15px;
                }}

                .modal-title {{
                    font-size: 1.2em;
                    font-weight: bold;
                    color: #2c3e50;
                }}

                .form-group {{
                    margin-bottom: 15px;
                }}

                .form-group label {{
                    display: block;
                    margin-bottom: 5px;
                    font-weight: 600;
                    color: #2c3e50;
                }}

                .form-group textarea {{
                    width: 100%;
                    padding: 10px;
                    border: 2px solid #ddd;
                    border-radius: 5px;
                    resize: vertical;
                    min-height: 80px;
                }}

                .modal-actions {{
                    display: flex;
                    gap: 10px;
                    justify-content: flex-end;
                }}

                /* Animations */
                @keyframes fadeIn {{
                    to {{
                        opacity: 1;
                    }}
                }}
                
                /* Auto-fulfilled request styling */
                .request-card.auto-fulfilled {{
                    border-left: 4px solid #2ecc71;
                    background: linear-gradient(145deg, #ffffff 0%, #f8fff9 100%);
                }}

                .auto-badge {{
                    font-size: 0.8em;
                    margin-left: 8px;
                    opacity: 0.9;
                }}

                .auto-fulfilled-badge {{
                    background: linear-gradient(45deg, #2ecc71, #27ae60);
                    color: white;
                    padding: 3px 8px;
                    border-radius: 12px;
                    font-size: 0.75em;
                    font-weight: 600;
                    display: inline-flex;
                    align-items: center;
                    gap: 4px;
                }}

                /* Mobile responsiveness */
                @media (max-width: 1200px) {{
                    .requests-grid {{
                        grid-template-columns: 1fr;
                    }}
                }}

                @media (max-width: 768px) {{
                    .controls {{
                        flex-direction: column;
                        align-items: stretch;
                    }}
                    
                    .stats {{
                        flex-wrap: wrap;
                        justify-content: center;
                    }}
                    
                    .request-main-content {{
                        flex-direction: column;
                    }}
                    
                    .request-cover {{
                        flex: none;
                        width: 100%;
                        min-height: 120px;
                        border-right: none;
                        border-bottom: 1px solid #e9ecef;
                        padding: 10px;
                    }}
                    
                    .request-cover img {{
                        max-height: 100px;
                    }}
                    
                    .request-metadata {{
                        padding: 12px;
                    }}
                    
                    .request-summary {{
                        padding: 0 15px 12px 15px;
                    }}
                    
                    .request-header {{
                        flex-direction: column;
                        align-items: flex-start;
                        gap: 8px;
                    }}
                    
                    .request-header-right {{
                        margin-left: 0;
                        align-self: flex-end;
                    }}
                    
                    .header-content {{
                        flex-direction: column;
                        gap: 15px;
                        text-align: center;
                    }}
                }}

                /* Enhanced grid for very large screens */
                @media (min-width: 3840px) {{
                    .requests-grid {{
                        grid-template-columns: repeat(3, 1fr);
                    }}
                    
                    .request-cover {{
                        flex: 0 0 300px;
                    }}
                    
                    .request-cover img {{
                        max-height: 190px;
                    }}
                    
                    .request-metadata {{
                        flex: 0 0 300px;
                }}
            </style>
        </head>
        <body>
            <div class="header">
                <div class="header-content">
                    <h1>🎮 ROM Requests Dashboard</h1>
                    <div class="stats">
                        <div class="stat-item">
                            <div class="stat-number" id="pending-count">-</div>
                            <div class="stat-label">Pending</div>
                        </div>
                        <div class="stat-item">
                            <div class="stat-number" id="fulfilled-count">-</div>
                            <div class="stat-label">Fulfilled</div>
                        </div>
                        <div class="stat-item">
                            <div class="stat-number" id="auto-fulfilled-count">-</div>
                            <div class="stat-label">Auto-Fulfilled</div>
                        </div>
                        <div class="stat-item">
                            <div class="stat-number" id="total-count">-</div>
                            <div class="stat-label">Total</div>
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="container">
                <div class="controls">
                    <div class="filter-group">
                        <label>Status:</label>
                        <select id="status-filter">
                            <option value="">All</option>
                            <option value="pending">Pending</option>
                            <option value="fulfilled">Fulfilled</option>
                            <option value="rejected">Rejected</option>
                            <option value="cancelled">Cancelled</option>
                        </select>
                    </div>
                    <div class="filter-group">
                        <label>Fulfillment:</label>
                        <select id="fulfillment-filter">
                            <option value="">All Methods</option>
                            <option value="auto">Auto-Fulfilled</option>
                            <option value="manual">Manual</option>
                        </select>
                    </div>
                    <div class="filter-group">
                        <label>Platform:</label>
                        <select id="platform-filter">
                            <option value="">All Platforms</option>
                        </select>
                    </div>
                    <div class="filter-group">
                        <label>Search:</label>
                        <input type="text" id="search-input" placeholder="Search games or users...">
                    </div>
                    <button class="btn btn-secondary" onclick="loadRequests()">Refresh</button>
                </div>
                
                <div class="loading" id="loading">Loading requests...</div>
                <div class="requests-grid" id="requests-container"></div>
            </div>
            
            <!-- Note Modal -->
            <div class="modal" id="note-modal">
                <div class="modal-content">
                    <div class="modal-header">
                        <h3 class="modal-title">Add Note</h3>
                    </div>
                    <div class="form-group">
                        <label for="note-text">Note:</label>
                        <textarea id="note-text" placeholder="Enter a note about this request..."></textarea>
                    </div>
                    <div class="modal-actions">
                        <button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
                        <button class="btn btn-success" onclick="submitNote()">Add Note</button>
                    </div>
                </div>
            </div>
            
            <script>
                let currentRequests = [];
                let currentRequestId = null;
                
                // Check authentication on page load
                document.addEventListener('DOMContentLoaded', function() {{
                    const token = localStorage.getItem('dashboardToken');
                    if (!token) {{
                        window.location.href = '/';
                        return;
                    }}
                    loadRequests();
                }});
                
                async function apiCall(endpoint, options = {{}}) {{
                    const token = localStorage.getItem('dashboardToken');
                    const defaultOptions = {{
                        headers: {{
                            'Authorization': `Bearer ${{token}}`,
                            'Content-Type': 'application/json',
                            ...options.headers
                        }}
                    }};
                    
                    const response = await fetch(endpoint, {{...defaultOptions, ...options}});
                    
                    if (response.status === 401) {{
                        localStorage.removeItem('dashboardToken');
                        window.location.href = '/';
                        return null;
                    }}
                    
                    return response;
                }}
                
                async function loadRequests() {{
                    document.getElementById('loading').style.display = 'block';
                    document.getElementById('requests-container').innerHTML = '';
                    
                    try {{
                        const response = await apiCall('/api/requests');
                        if (response && response.ok) {{
                            currentRequests = await response.json();
                            renderRequests();
                            updateStats();
                            updateFilters();
                        }}
                    }} catch (error) {{
                        console.error('Error loading requests:', error);
                    }} finally {{
                        document.getElementById('loading').style.display = 'none';
                    }}
                }}
                
                function renderRequests() {{
                    const container = document.getElementById('requests-container');
                    const statusFilter = document.getElementById('status-filter').value;
                    const fulfillmentFilter = document.getElementById('fulfillment-filter').value;
                    const platformFilter = document.getElementById('platform-filter').value;
                    const searchTerm = document.getElementById('search-input').value.toLowerCase();
                    
                    let filteredRequests = currentRequests.filter(req => {{
                        const matchesStatus = !statusFilter || req.status === statusFilter;
                        const matchesPlatform = !platformFilter || req.platform === platformFilter;
                        const matchesSearch = !searchTerm || 
                            req.game_name.toLowerCase().includes(searchTerm) ||
                            req.username.toLowerCase().includes(searchTerm) ||
                            (req.details && req.details.toLowerCase().includes(searchTerm));
                        
                        // Filter by fulfillment method
                        let matchesFulfillment = true;
                        if (fulfillmentFilter === 'auto') {{
                            matchesFulfillment = req.auto_fulfilled === true;
                        }} else if (fulfillmentFilter === 'manual') {{
                            matchesFulfillment = req.status === 'fulfilled' && req.auto_fulfilled === false;
                        }}
                        
                        return matchesStatus && matchesPlatform && matchesSearch && matchesFulfillment;
                    }});
                    
                    if (filteredRequests.length === 0) {{
                        container.innerHTML = '<div class="loading">No requests found matching your criteria.</div>';
                        return;
                    }}
                    
                    container.innerHTML = filteredRequests.map(req => createRequestCard(req)).join('');
                }}
                                
                function createRequestCard(request) {{
                    const createdDate = new Date(request.created_at).toLocaleDateString();
                    const isPending = request.status === 'pending';
                    
                    // Use IGDB data if available, otherwise fall back to request data
                    const displayName = request.igdb?.name || request.game_name;
                    const coverUrl = request.igdb?.cover_url;
                    const summary = request.igdb?.summary;
                    const releaseDate = request.igdb?.release_date;
                    const genres = request.igdb?.genres;
                    const developers = request.igdb?.developers;
                    const publishers = request.igdb?.publishers;
                    
                    // Format release date
                    let formattedDate = 'Unknown';
                    if (releaseDate && releaseDate !== 'Unknown') {{
                        try {{
                            const dateObj = new Date(releaseDate);
                            formattedDate = dateObj.toLocaleDateString('en-US', {{
                                year: 'numeric',
                                month: 'long',
                                day: 'numeric'
                            }});
                        }} catch {{
                            formattedDate = releaseDate;
                        }}
                    }}
                    
                    // Create IGDB link
                    const igdbName = displayName.toLowerCase().replace(/[^a-z0-9\\s]/g, '').replace(/\\s+/g, '-');
                    const igdbUrl = `https://www.igdb.com/games/${{igdbName}}`;
                    
                    // Format non-IGDB details (exclude IGDB metadata)
                    let userDetails = '';
                    if (request.details) {{
                        if (request.details.includes('IGDB Metadata:')) {{
                            userDetails = request.details.split('IGDB Metadata:')[0].trim();
                        }} else {{
                            userDetails = request.details;
                        }}
                    }}
                    
                    // Determine fulfillment method for display
                    let fulfillmentInfo = '';
                    if (request.status === 'fulfilled') {{
                        if (request.auto_fulfilled) {{
                            fulfillmentInfo = `
                                <div class="request-field">
                                    <div class="field-label">Fulfillment:</div>
                                    <div class="field-value">
                                        <span class="auto-fulfilled-badge">🤖 Auto-Fulfilled</span>
                                    </div>
                                </div>
                            `;
                        }} else if (request.fulfiller_name) {{
                            fulfillmentInfo = `
                                <div class="request-field">
                                    <div class="field-label">Fulfilled by:</div>
                                    <div class="field-value">${{request.fulfiller_name}}</div>
                                </div>
                            `;
                        }}
                    }}
                    
                    return `
                        <div class="request-card ${{request.igdb ? 'has-igdb' : 'no-igdb'}} ${{request.auto_fulfilled ? 'auto-fulfilled' : ''}}">
                            <div class="request-header">
                                <div class="request-header-left">
                                    <div class="request-title">
                                        ${{displayName}}
                                        ${{request.auto_fulfilled ? '<span class="auto-badge">🤖</span>' : ''}}
                                    </div>
                                    <div class="request-meta">Request #${{request.id}} • ${{createdDate}}</div>
                                </div>
                                <div class="request-header-right">
                                    <span class="status-badge status-${{request.status}}">${{request.status}}</span>
                                </div>
                            </div>
                            
                            <div class="request-content-wrapper">
                                <div class="request-main-content">
                                    ${{coverUrl ? `
                                        <div class="request-cover">
                                            <img src="${{coverUrl}}" alt="${{displayName}} cover" onerror="this.style.display='none'">
                                        </div>
                                    ` : ''}}
                                    
                                    <div class="request-metadata">
                                        <div class="request-field">
                                            <div class="field-label">Requested by:</div>
                                            <div class="field-value">${{request.username}}</div>
                                        </div>
                                        
                                        <div class="request-field">
                                            <div class="field-label">Platform:</div>
                                            <div class="field-value">${{request.platform}}</div>
                                        </div>
                                        
                                        ${{releaseDate && releaseDate !== 'Unknown' ? `
                                            <div class="request-field">
                                                <div class="field-label">Release Date:</div>
                                                <div class="field-value">${{formattedDate}}</div>
                                            </div>
                                        ` : ''}}
                                        
                                        ${{genres && genres.length > 0 ? `
                                            <div class="request-field">
                                                <div class="field-label">Genres:</div>
                                                <div class="field-value">${{genres.slice(0, 2).join(', ')}}</div>
                                            </div>
                                        ` : ''}}
                                        
                                        ${{developers && developers.length > 0 && developers[0] !== 'Unknown' ? `
                                            <div class="request-field">
                                                <div class="field-label">Developer:</div>
                                                <div class="field-value">${{developers.slice(0, 1).join(', ')}}</div>
                                            </div>
                                        ` : ''}}
                                        
                                        ${{userDetails ? `
                                            <div class="request-field">
                                                <div class="field-label">Details:</div>
                                                <div class="field-value">${{userDetails.length > 80 ? userDetails.substring(0, 80) + '...' : userDetails}}</div>
                                            </div>
                                        ` : ''}}
                                        
                                        ${{fulfillmentInfo}}
                                        
                                        ${{request.notes ? `
                                            <div class="request-field">
                                                <div class="field-label">Notes:</div>
                                                <div class="field-value">${{request.notes.length > 60 ? request.notes.substring(0, 60) + '...' : request.notes}}</div>
                                            </div>
                                        ` : ''}}
                                        
                                        ${{request.igdb ? `
                                            <div class="request-field">
                                                <div class="field-label">Links:</div>
                                                <div class="field-value">
                                                    <a href="${{igdbUrl}}" target="_blank" class="igdb-link">IGDB</a>
                                                </div>
                                            </div>
                                        ` : ''}}
                                    </div>
                                </div>
                                
                                ${{summary ? `
                                    <div class="request-summary">
                                        <div class="field-label">Summary:</div>
                                        <div class="summary-text">${{summary.length > 200 ? summary.substring(0, 200) + '...' : summary}}</div>
                                    </div>
                                ` : ''}}
                            </div>
                            
                            <div class="request-actions">
                                <button class="btn btn-success" onclick="fulfillRequest(${{request.id}})" ${{!isPending ? 'disabled' : ''}}>
                                    Fulfill
                                </button>
                                <button class="btn btn-danger" onclick="rejectRequest(${{request.id}})" ${{!isPending ? 'disabled' : ''}}>
                                    Reject
                                </button>
                                <button class="btn btn-warning" onclick="deleteRequest(${{request.id}})">
                                    Delete
                                </button>
                                <button class="btn btn-secondary" onclick="openNoteModal(${{request.id}})">
                                    Note
                                </button>
                            </div>
                        </div>
                    `;
                }}
                
                function updateStats() {{
                    const stats = currentRequests.reduce((acc, req) => {{
                        acc[req.status] = (acc[req.status] || 0) + 1;
                        acc.total++;
                        
                        // Count auto-fulfilled requests
                        if (req.auto_fulfilled) {{
                            acc.autoFulfilled = (acc.autoFulfilled || 0) + 1;
                        }}
                        
                        return acc;
                    }}, {{ total: 0 }});
                    
                    document.getElementById('pending-count').textContent = stats.pending || 0;
                    document.getElementById('fulfilled-count').textContent = stats.fulfilled || 0;
                    document.getElementById('auto-fulfilled-count').textContent = stats.autoFulfilled || 0;
                    document.getElementById('total-count').textContent = stats.total;
                    document.getElementById('fulfillment-filter').addEventListener('change', renderRequests);
                }}
                
                function updateFilters() {{
                    const platforms = [...new Set(currentRequests.map(req => req.platform))].sort();
                    const platformFilter = document.getElementById('platform-filter');
                    
                    // Keep current selection
                    const currentSelection = platformFilter.value;
                    
                    platformFilter.innerHTML = '<option value="">All Platforms</option>' +
                        platforms.map(platform => `<option value="${{platform}}">${{platform}}</option>`).join('');
                    
                    // Restore selection if it still exists
                    if (platforms.includes(currentSelection)) {{
                        platformFilter.value = currentSelection;
                    }}
                }}
                                
                async function deleteRequest(requestId) {{
                    if (!confirm('Are you sure you want to permanently delete this request? This action cannot be undone.')) return;
                    
                    try {{
                        const response = await apiCall(`/api/requests/${{requestId}}/delete`, {{
                            method: 'DELETE'
                        }});
                        
                        if (response && response.ok) {{
                            await loadRequests();
                        }} else {{
                            alert('Failed to delete request');
                        }}
                    }} catch (error) {{
                        console.error('Error deleting request:', error);
                        alert('Error deleting request');
                    }}
                }}
                
                async function fulfillRequest(requestId) {{
                    if (!confirm('Mark this request as fulfilled?')) return;
                    
                    try {{
                        const response = await apiCall(`/api/requests/${{requestId}}/fulfill`, {{
                            method: 'POST'
                        }});
                        
                        if (response && response.ok) {{
                            await loadRequests();
                        }} else {{
                            alert('Failed to fulfill request');
                        }}
                    }} catch (error) {{
                        console.error('Error fulfilling request:', error);
                        alert('Error fulfilling request');
                    }}
                }}
                
                async function rejectRequest(requestId) {{
                    const reason = prompt('Reason for rejection (optional):');
                    if (reason === null) return; // User cancelled
                    
                    try {{
                        const response = await apiCall(`/api/requests/${{requestId}}/reject`, {{
                            method: 'POST',
                            body: JSON.stringify({{ reason }})
                        }});
                        
                        if (response && response.ok) {{
                            await loadRequests();
                        }} else {{
                            alert('Failed to reject request');
                        }}
                    }} catch (error) {{
                        console.error('Error rejecting request:', error);
                        alert('Error rejecting request');
                    }}
                }}
                
                function openNoteModal(requestId) {{
                    currentRequestId = requestId;
                    document.getElementById('note-text').value = '';
                    document.getElementById('note-modal').style.display = 'block';
                }}
                
                function closeModal() {{
                    document.getElementById('note-modal').style.display = 'none';
                    currentRequestId = null;
                }}
                
                async function submitNote() {{
                    const noteText = document.getElementById('note-text').value.trim();
                    if (!noteText) {{
                        alert('Please enter a note');
                        return;
                    }}
                    
                    try {{
                        const response = await apiCall(`/api/requests/${{currentRequestId}}/note`, {{
                            method: 'POST',
                            body: JSON.stringify({{ note: noteText }})
                        }});
                        
                        if (response && response.ok) {{
                            closeModal();
                            await loadRequests();
                        }} else {{
                            alert('Failed to add note');
                        }}
                    }} catch (error) {{
                        console.error('Error adding note:', error);
                        alert('Error adding note');
                    }}
                }}
                
                // Event listeners for filters
                document.getElementById('status-filter').addEventListener('change', renderRequests);
                document.getElementById('platform-filter').addEventListener('change', renderRequests);
                document.getElementById('search-input').addEventListener('input', renderRequests);
                
                // Close modal when clicking outside
                document.getElementById('note-modal').addEventListener('click', function(e) {{
                    if (e.target === this) {{
                        closeModal();
                    }}
                }});
            </script>
        </body>
        </html>
        """
        return web.Response(text=html, content_type='text/html')
    
    async def api_get_requests(self, request: web_request.Request):
        """API endpoint to get all requests with parsed IGDB metadata"""
        if not self.check_auth(request):
            return web.Response(status=401, text='Unauthorized')
        
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    SELECT id, user_id, username, platform, game_name, details, 
                           status, created_at, updated_at, fulfilled_by, fulfiller_name, notes, auto_fulfilled
                    FROM requests 
                    ORDER BY created_at DESC
                """)
                rows = await cursor.fetchall()
                
                requests = []
                for row in rows:
                    request_data = {
                        'id': row[0],
                        'user_id': row[1],
                        'username': row[2],
                        'platform': row[3],
                        'game_name': row[4],
                        'details': row[5],
                        'status': row[6],
                        'created_at': row[7],
                        'updated_at': row[8],
                        'fulfilled_by': row[9],
                        'fulfiller_name': row[10],
                        'notes': row[11],
                        'auto_fulfilled': bool(row[12])
                    }
                    
                    # Parse IGDB metadata if available
                    igdb_data = self.parse_igdb_metadata(row[5])
                    if igdb_data:
                        request_data['igdb'] = igdb_data
                    
                    requests.append(request_data)
                
                return web.json_response(requests)
                
        except Exception as e:
            logger.error(f"Error fetching requests: {e}")
            return web.Response(status=500, text='Internal server error')

    def parse_igdb_metadata(self, details: str) -> Optional[Dict]:
        """Parse IGDB metadata from request details"""
        if not details or "IGDB Metadata:" not in details:
            return None
        
        try:
            # Extract IGDB metadata section
            metadata_section = details.split("IGDB Metadata:\n")[1]
            
            # Parse each line
            igdb_data = {}
            lines = metadata_section.split("\n")
            
            for line in lines:
                if ": " in line:
                    key, value = line.split(": ", 1)
                    igdb_data[key.strip()] = value.strip()
            
            # Clean up the data
            parsed_data = {
                'name': igdb_data.get('Game', ''),
                'release_date': igdb_data.get('Release Date', ''),
                'platforms': igdb_data.get('Platforms', '').split(', ') if igdb_data.get('Platforms') else [],
                'developers': igdb_data.get('Developers', '').split(', ') if igdb_data.get('Developers') else [],
                'publishers': igdb_data.get('Publishers', '').split(', ') if igdb_data.get('Publishers') else [],
                'genres': igdb_data.get('Genres', '').split(', ') if igdb_data.get('Genres') else [],
                'game_modes': igdb_data.get('Game Modes', '').split(', ') if igdb_data.get('Game Modes') else [],
                'summary': igdb_data.get('Summary', ''),
                'cover_url': igdb_data.get('Cover URL', '') if igdb_data.get('Cover URL') != 'None' else None
            }
            
            # Filter out empty values
            return {k: v for k, v in parsed_data.items() if v}
            
        except Exception as e:
            logger.error(f"Error parsing IGDB metadata: {e}")
            return None
    
    async def api_fulfill_request(self, request: web_request.Request):
        """API endpoint to fulfill a request"""
        if not self.check_auth(request):
            return web.Response(status=401, text='Unauthorized')
        
        try:
            request_id = int(request.match_info['request_id'])
            
            async with aiosqlite.connect(self.db_path) as db:
                # Update request status
                await db.execute("""
                    UPDATE requests 
                    SET status = 'fulfilled', 
                        fulfilled_by = 0, 
                        fulfiller_name = 'Admin', 
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status = 'pending'
                """, (request_id,))
                
                # Check if any rows were affected
                if db.total_changes == 0:
                    return web.Response(status=404, text='Request not found or not pending')
                
                await db.commit()
                
                # Get request details to notify user
                cursor = await db.execute("""
                    SELECT user_id, game_name FROM requests WHERE id = ?
                """, (request_id,))
                req = await cursor.fetchone()
                
                if req:
                    try:
                        user = await self.bot.fetch_user(req[0])
                        await user.send(f"✅ Your request for '{req[1]}' has been fulfilled by an admin!")
                    except Exception as e:
                        logger.warning(f"Could not notify user {req[0]}: {e}")
                
                return web.json_response({'status': 'success'})
                
        except ValueError:
            return web.Response(status=400, text='Invalid request ID')
        except Exception as e:
            logger.error(f"Error fulfilling request: {e}")
            return web.Response(status=500, text='Internal server error')
    
    async def api_reject_request(self, request: web_request.Request):
        """API endpoint to reject a request"""
        if not self.check_auth(request):
            return web.Response(status=401, text='Unauthorized')
        
        try:
            request_id = int(request.match_info['request_id'])
            data = await request.json() if request.content_type == 'application/json' else {}
            reason = data.get('reason', '')
            
            async with aiosqlite.connect(self.db_path) as db:
                # Update request status
                await db.execute("""
                    UPDATE requests 
                    SET status = 'rejected', 
                        fulfilled_by = 0, 
                        fulfiller_name = 'Admin', 
                        updated_at = CURRENT_TIMESTAMP,
                        notes = ?
                    WHERE id = ? AND status = 'pending'
                """, (reason, request_id))
                
                # Check if any rows were affected
                if db.total_changes == 0:
                    return web.Response(status=404, text='Request not found or not pending')
                
                await db.commit()
                
                # Get request details to notify user
                cursor = await db.execute("""
                    SELECT user_id, game_name FROM requests WHERE id = ?
                """, (request_id,))
                req = await cursor.fetchone()
                
                if req:
                    try:
                        user = await self.bot.fetch_user(req[0])
                        message = f"❌ Your request for '{req[1]}' has been rejected by an admin."
                        if reason:
                            message += f"\nReason: {reason}"
                        await user.send(message)
                    except Exception as e:
                        logger.warning(f"Could not notify user {req[0]}: {e}")
                
                return web.json_response({'status': 'success'})
                
        except ValueError:
            return web.Response(status=400, text='Invalid request ID')
        except Exception as e:
            logger.error(f"Error rejecting request: {e}")
            return web.Response(status=500, text='Internal server error')
    
    async def api_delete_request(self, request: web_request.Request):
        """API endpoint to delete a request"""
        if not self.check_auth(request):
            return web.Response(status=401, text='Unauthorized')
        
        try:
            request_id = int(request.match_info['request_id'])
            
            async with aiosqlite.connect(self.db_path) as db:
                # Check if request exists first
                cursor = await db.execute("""
                    SELECT user_id, game_name FROM requests WHERE id = ?
                """, (request_id,))
                req = await cursor.fetchone()
                
                if not req:
                    return web.Response(status=404, text='Request not found')
                
                # Delete the request
                await db.execute("DELETE FROM requests WHERE id = ?", (request_id,))
                await db.commit()
                
                # Optional: Notify user that their request was deleted
                try:
                    user = await self.bot.fetch_user(req[0])
                    await user.send(f"🗑️ Your request for '{req[1]}' has been deleted by an administrator.")
                except Exception as e:
                    logger.warning(f"Could not notify user {req[0]} about deletion: {e}")
                
                return web.json_response({'status': 'success'})
                
        except ValueError:
            return web.Response(status=400, text='Invalid request ID')
        except Exception as e:
            logger.error(f"Error deleting request: {e}")
            return web.Response(status=500, text='Internal server error')
    
    async def api_add_note(self, request: web_request.Request):
        """API endpoint to add a note to a request"""
        if not self.check_auth(request):
            return web.Response(status=401, text='Unauthorized')
        
        try:
            request_id = int(request.match_info['request_id'])
            data = await request.json()
            note = data.get('note', '').strip()
            
            if not note:
                return web.Response(status=400, text='Note cannot be empty')
            
            async with aiosqlite.connect(self.db_path) as db:
                # Get existing notes
                cursor = await db.execute("SELECT notes FROM requests WHERE id = ?", (request_id,))
                result = await cursor.fetchone()
                
                if not result:
                    return web.Response(status=404, text='Request not found')
                
                existing_notes = result[0] or ''
                timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
                new_note = f"[{timestamp}] Admin: {note}"
                
                if existing_notes:
                    updated_notes = f"{existing_notes}\n{new_note}"
                else:
                    updated_notes = new_note
                
                # Update notes
                await db.execute("""
                    UPDATE requests 
                    SET notes = ?, updated_at = CURRENT_TIMESTAMP 
                    WHERE id = ?
                """, (updated_notes, request_id))
                
                await db.commit()
                
                return web.json_response({'status': 'success'})
                
        except ValueError:
            return web.Response(status=400, text='Invalid request ID')
        except Exception as e:
            logger.error(f"Error adding note: {e}")
            return web.Response(status=500, text='Internal server error')
    
    async def serve_static(self, request: web_request.Request):
        """Serve static files (if needed)"""
        filename = request.match_info['filename']
        # Basic security check
        if '..' in filename or filename.startswith('/'):
            return web.Response(status=404)
        
        # You can add static file serving here if needed
        return web.Response(status=404, text='Not found')
    
    def cog_unload(self):
        """Clean up when cog is unloaded"""
        if self.web_enabled:
            asyncio.create_task(self.stop_web_server())

def setup(bot):
    bot.add_cog(WebDashboard(bot))