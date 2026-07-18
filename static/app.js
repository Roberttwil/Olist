document.addEventListener('DOMContentLoaded', () => {
    // ----------------------------------------
    // STATE & DOMELEMENTS
    // ----------------------------------------
    const navButtons = document.querySelectorAll('.nav-btn');
    const tabContents = document.querySelectorAll('.tab-content');
    
    // Chatbot Elements
    const chatForm = document.getElementById('chat-form');
    const chatInput = document.getElementById('chat-input');
    const chatMessages = document.getElementById('chat-messages');
    const typingIndicator = document.getElementById('typing-indicator');
    const suggestButtons = document.querySelectorAll('.suggest-btn');
    
    // Explorer Elements
    const tablesList = document.getElementById('tables-list');
    const tableDetailsContainer = document.getElementById('table-details-container');
    const sqlEditorTriggerBtn = document.getElementById('sql-editor-trigger-btn');
    const explorerSidebarTitle = document.getElementById('explorer-sidebar-title');

    // Active state
    let activeTab = 'chat-tab';
    let threadId = sessionStorage.getItem('olist_chat_thread_id');
    if (!threadId) {
        threadId = 'session_' + Math.random().toString(36).substr(2, 9);
        sessionStorage.setItem('olist_chat_thread_id', threadId);
    }
    let activeConfirmContainer = null;

    // Helper authenticated fetch wrapper to auto-inject and prompt for passcode
    async function authenticatedFetch(url, options = {}) {
        let passcode = localStorage.getItem('olist_demo_passcode') || '';
        options.headers = options.headers || {};
        options.headers['X-Demo-Passcode'] = passcode;

        let response = await fetch(url, options);

        if (response.status === 401) {
            const newPasscode = prompt("Access Locked: Enter the demo passcode to continue:");
            if (newPasscode !== null) {
                localStorage.setItem('olist_demo_passcode', newPasscode);
                options.headers['X-Demo-Passcode'] = newPasscode;
                return await fetch(url, options);
            }
        }
        return response;
    }

    // ----------------------------------------
    // TAB SWITCHING
    // ----------------------------------------
    navButtons.forEach(btn => {
        btn.addEventListener('click', () => {
            const targetTab = btn.getAttribute('data-tab');
            if (targetTab === activeTab) return;

            // Update active buttons
            navButtons.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');

            // Update active tabs
            tabContents.forEach(tab => tab.classList.remove('active'));
            document.getElementById(targetTab).classList.add('active');

            activeTab = targetTab;

            // Load tables list if switched to explorer
            if (activeTab === 'explorer-tab') {
                loadTablesList();
            }
        });
    });

    // ----------------------------------------
    // CHATBOT FUNCTIONS
    // ----------------------------------------
    
    // Append a message to the chat container
    function appendMessage(sender, content, extra = null) {
        const messageDiv = document.createElement('div');
        messageDiv.classList.add('message', `${sender}-message`);

        const avatar = document.createElement('div');
        avatar.classList.add('message-avatar');
        avatar.innerHTML = sender === 'bot' ? '<i class="fa-solid fa-robot"></i>' : '<i class="fa-solid fa-user"></i>';

        const bubble = document.createElement('div');
        bubble.classList.add('message-bubble');

        if (sender === 'user') {
            bubble.textContent = content;
        } else {
            // It's from bot
            const answerText = document.createElement('div');
            answerText.innerHTML = formatMarkdown(content);
            bubble.appendChild(answerText);

            // Add SQL query details if present
            if (extra && extra.generated_sql) {
                const accordion = document.createElement('div');
                accordion.classList.add('sql-accordion');

                const header = document.createElement('div');
                header.classList.add('sql-header');
                header.innerHTML = `<span><i class="fa-solid fa-code"></i> View Executed SQL Query</span> <i class="fa-solid fa-chevron-down"></i>`;
                
                const codeBlock = document.createElement('div');
                codeBlock.classList.add('sql-content');
                codeBlock.innerHTML = `<pre><code>${escapeHTML(extra.generated_sql)}</code></pre>`;

                header.addEventListener('click', () => {
                    codeBlock.classList.toggle('open');
                    const icon = header.querySelector('.fa-chevron-down, .fa-chevron-up');
                    if (icon) {
                        icon.classList.toggle('fa-chevron-down');
                        icon.classList.toggle('fa-chevron-up');
                    }
                });

                accordion.appendChild(header);
                accordion.appendChild(codeBlock);
                bubble.appendChild(accordion);
            }

            // Add confirm action buttons if awaiting approval
            if (extra && extra.status === 'awaiting_approval') {
                const confirmContainer = document.createElement('div');
                activeConfirmContainer = confirmContainer;
                confirmContainer.classList.add('confirm-btn-container');
                confirmContainer.style.display = 'flex';
                confirmContainer.style.gap = '12px';
                confirmContainer.style.marginTop = '14px';

                const executeBtn = document.createElement('button');
                executeBtn.innerHTML = '<i class="fa-solid fa-play"></i> Jalankan Query';
                executeBtn.style.padding = '8px 16px';
                executeBtn.style.fontSize = '12px';
                executeBtn.style.fontWeight = '600';
                executeBtn.style.borderRadius = '8px';
                executeBtn.style.border = 'none';
                executeBtn.style.background = 'var(--primary)';
                executeBtn.style.color = '#ffffff';
                executeBtn.style.cursor = 'pointer';

                const cancelBtn = document.createElement('button');
                cancelBtn.innerHTML = '<i class="fa-solid fa-xmark"></i> Batalkan';
                cancelBtn.style.padding = '8px 16px';
                cancelBtn.style.fontSize = '12px';
                cancelBtn.style.fontWeight = '600';
                cancelBtn.style.borderRadius = '8px';
                cancelBtn.style.border = '1px solid var(--border-color)';
                cancelBtn.style.background = '#ffffff';
                cancelBtn.style.color = 'var(--text-muted)';
                cancelBtn.style.cursor = 'pointer';

                executeBtn.addEventListener('click', async () => {
                    confirmContainer.innerHTML = '<span class="status-text" style="color: var(--primary); font-weight:600;"><i class="fa-solid fa-spinner fa-spin"></i> Menjalankan query di RDS...</span>';
                    await sendConfirmation(true);
                });

                cancelBtn.addEventListener('click', async () => {
                    confirmContainer.innerHTML = '<span class="status-text" style="color: var(--text-muted); font-weight:600;"><i class="fa-solid fa-xmark"></i> Membatalkan...</span>';
                    await sendConfirmation(false);
                });

                confirmContainer.appendChild(executeBtn);
                confirmContainer.appendChild(cancelBtn);
                bubble.appendChild(confirmContainer);
            }

            // Add badges (success, self-healing retries) for normal complete messages
            if (extra && extra.status !== 'awaiting_approval' && extra.generated_sql) {
                const badgeContainer = document.createElement('div');
                badgeContainer.classList.add('badge-container');

                if (extra.retry_count > 0) {
                    const healBadge = document.createElement('span');
                    healBadge.classList.add('badge', 'badge-heal');
                    healBadge.innerHTML = `<i class="fa-solid fa-heart-pulse"></i> Self-Healed (${extra.retry_count} retries)`;
                    badgeContainer.appendChild(healBadge);
                }

                if (!extra.error_message) {
                    const successBadge = document.createElement('span');
                    successBadge.classList.add('badge', 'badge-success');
                    successBadge.innerHTML = `<i class="fa-solid fa-circle-check"></i> Query Succeeded`;
                    badgeContainer.appendChild(successBadge);
                }
                
                if (badgeContainer.children.length > 0) {
                    bubble.appendChild(badgeContainer);
                }
            }
        }

        messageDiv.appendChild(avatar);
        messageDiv.appendChild(bubble);
        chatMessages.appendChild(messageDiv);
        
        // Scroll to bottom
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    // Submit question to FastAPI
    async function submitQuestion(question) {
        if (typingIndicator.style.display === 'flex') return;
        
        // Clear input field
        chatInput.value = '';
        
        // Append user message
        appendMessage('user', question);

        // Show typing indicator
        typingIndicator.style.display = 'flex';
        chatMessages.scrollTop = chatMessages.scrollHeight;

        try {
            const response = await authenticatedFetch('/api/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ question: question, thread_id: threadId })
            });

            if (!response.ok) {
                const err = await response.json();
                throw new Error(err.detail || 'API request failed');
            }

            const data = await response.json();
            threadId = data.thread_id;
            
            // Hide typing
            typingIndicator.style.display = 'none';

            // Append bot response
            appendMessage('bot', data.final_answer, {
                generated_sql: data.generated_sql,
                retry_count: data.retry_count,
                error_message: data.error_message,
                status: data.status
            });

            // Update/clear active confirmation container if status resolved
            if (activeConfirmContainer) {
                if (data.status === 'completed') {
                    activeConfirmContainer.innerHTML = '<span class="status-text" style="color: var(--success); font-weight:600;"><i class="fa-solid fa-circle-check"></i> Query Executed</span>';
                    activeConfirmContainer = null;
                } else if (data.status === 'canceled') {
                    activeConfirmContainer.innerHTML = '<span class="status-text" style="color: var(--error); font-weight:600;"><i class="fa-solid fa-circle-xmark"></i> Canceled</span>';
                    activeConfirmContainer = null;
                }
            }

        } catch (error) {
            typingIndicator.style.display = 'none';
            appendMessage('bot', `Sorry, I encountered an error: **${error.message}**\nPlease verify your AWS RDS connection and API keys in your \`.env\` file.`);
        }
    }

    // Send confirmation to execute or cancel SQL
    async function sendConfirmation(confirm) {
        if (typingIndicator.style.display === 'flex') return;
        
        typingIndicator.style.display = 'flex';
        chatMessages.scrollTop = chatMessages.scrollHeight;

        try {
            const response = await authenticatedFetch('/api/chat/confirm', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ thread_id: threadId, confirm: confirm })
            });

            if (!response.ok) {
                const err = await response.json();
                throw new Error(err.detail || 'Confirmation request failed');
            }

            const data = await response.json();
            typingIndicator.style.display = 'none';

            appendMessage('bot', data.final_answer, {
                generated_sql: data.generated_sql,
                retry_count: data.retry_count,
                error_message: data.error_message,
                status: data.status
            });

            // Update/clear active confirmation container
            if (activeConfirmContainer) {
                if (data.status === 'completed') {
                    activeConfirmContainer.innerHTML = '<span class="status-text" style="color: var(--success); font-weight:600;"><i class="fa-solid fa-circle-check"></i> Query Executed</span>';
                } else if (data.status === 'canceled') {
                    activeConfirmContainer.innerHTML = '<span class="status-text" style="color: var(--error); font-weight:600;"><i class="fa-solid fa-circle-xmark"></i> Canceled</span>';
                }
                activeConfirmContainer = null;
            }

        } catch (error) {
            typingIndicator.style.display = 'none';
            appendMessage('bot', `An error occurred during confirmation: **${error.message}**`);
        }
    }

    // Form submission
    chatForm.addEventListener('submit', (e) => {
        e.preventDefault();
        const text = chatInput.value.strip ? chatInput.value.strip() : chatInput.value.trim();
        if (text) {
            submitQuestion(text);
        }
    });

    // Suggestions click
    suggestButtons.forEach(btn => {
        btn.addEventListener('click', () => {
            submitQuestion(btn.textContent);
        });
    });

    // ----------------------------------------
    // DATABASE EXPLORER FUNCTIONS
    // ----------------------------------------
    let tablesData = [];

    // Load tables list in sidebar
    async function loadTablesList() {
        tablesList.innerHTML = '<div class="tables-loading"><i class="fa-solid fa-spinner fa-spin"></i> Loading tables...</div>';
        
        try {
            const response = await authenticatedFetch('/api/tables');
            if (!response.ok) throw new Error('Failed to load tables');
            tablesData = await response.json();

            tablesList.innerHTML = '';
            tablesData.forEach(table => {
                const item = document.createElement('div');
                item.classList.add('table-item');
                item.setAttribute('data-table', table.name);
                item.innerHTML = `
                    <span><i class="fa-solid fa-table"></i> ${table.name}</span>
                    <span class="row-badge">${table.row_count.toLocaleString()} rows</span>
                `;

                item.addEventListener('click', () => {
                    // Update active table
                    document.querySelectorAll('.table-item').forEach(i => i.classList.remove('active'));
                    if (sqlEditorTriggerBtn) sqlEditorTriggerBtn.classList.remove('active');
                    item.classList.add('active');

                    // Load specific table details
                    loadTableDetails(table.name);
                });

                tablesList.appendChild(item);
            });

            // Trigger staggered background prefetch of all table schemas and page 1 data
            tablesData.forEach((table, index) => {
                setTimeout(() => {
                    prefetchTableDetails(table.name);
                }, index * 450); // Stagger requests to avoid concurrent database load spikes
            });

        } catch (error) {
            tablesList.innerHTML = `<div style="color: var(--error); font-size: 12px; padding: 10px;"><i class="fa-solid fa-triangle-exclamation"></i> Error: ${error.message}</div>`;
        }
    }

    let tableCache = {};

    // Background prefetch task to retrieve schemas and first page of data for all tables
    async function prefetchTableDetails(tableName) {
        if (tableCache[tableName]) return; // Already cached
        
        try {
            const response = await authenticatedFetch(`/api/tables/${tableName}?page=1&page_size=50`);
            if (response.ok) {
                const data = await response.json();
                tableCache[tableName] = {
                    name: data.name,
                    columns: data.columns,
                    foreignKeys: data.foreign_keys,
                    totalRows: data.pagination.total_rows,
                    rows: {
                        1: data.preview_data
                    },
                    prefetchedUntilPage: 1
                };
            }
        } catch (err) {
            console.warn(`[Background DDL Prefetch] Failed for ${tableName}:`, err);
        }
    }

    // Parallel Background prefetch task to retrieve page sets concurrently
    function prefetchTablePages(tableName, startPage, endPage) {
        if (!tableCache[tableName]) return;
        
        const cache = tableCache[tableName];
        const totalPages = Math.ceil(cache.totalRows / 50);
        const limitPage = Math.min(endPage, totalPages);
        
        if (startPage > limitPage) return;
        
        // Track the highest page we have queued
        cache.prefetchedUntilPage = Math.max(cache.prefetchedUntilPage || 1, limitPage);
        
        for (let p = startPage; p <= limitPage; p++) {
            if (cache.rows[p]) continue; // Already cached, skip
            
            // Fire request concurrently in the background (no await block)
            authenticatedFetch(`/api/tables/${tableName}/data?page=${p}&page_size=50`)
                .then(res => {
                    if (res.ok) return res.json();
                    throw new Error(`HTTP ${res.status}`);
                })
                .then(data => {
                    cache.rows[p] = data.preview_data;
                })
                .catch(err => {
                    console.warn(`[Prefetch] Failed for ${tableName} page ${p}:`, err);
                });
        }
    }

    // Check if we need to prefetch the next batch of 10 pages in the background
    function triggerConditionalPrefetch(tableName, currentPage) {
        const cache = tableCache[tableName];
        if (!cache) return;
        
        if (currentPage >= cache.prefetchedUntilPage - 3) {
            const startPrefetch = cache.prefetchedUntilPage + 1;
            const endPrefetch = startPrefetch + 9;
            prefetchTablePages(tableName, startPrefetch, endPrefetch);
        }
    }

    // Render the structural layout (Headers, Columns, FKs) without the rows
    function renderTableLayout(data) {
        let html = `
            <div class="table-details-header">
                <div class="table-title">
                    <h2>${data.name}</h2>
                    <p>Schema definition, foreign keys, and paginated data preview</p>
                </div>
            </div>

            <!-- Section 1: Columns Schema -->
            <div class="details-section">
                <h3><i class="fa-solid fa-table-columns"></i> Schema Definition</h3>
                <div class="schema-table-wrapper">
                    <table class="schema-table">
                        <thead>
                            <tr>
                                <th>Column Name</th>
                                <th>Data Type</th>
                                <th>Nullable</th>
                            </tr>
                        </thead>
                        <tbody>
        `;

        data.columns.forEach(col => {
            const nullableStr = col.nullable ? 'YES' : 'NO';
            const nullableClass = col.nullable ? '' : 'not-null';
            html += `
                <tr>
                    <td style="font-weight: 600;">${col.name}</td>
                    <td><span class="type-pill">${col.type}</span></td>
                    <td><span class="nullable-badge ${nullableClass}">${nullableStr}</span></td>
                </tr>
            `;
        });

        html += `
                        </tbody>
                    </table>
                </div>
            </div>
        `;

        // Section 2: Foreign Keys
        if (data.foreignKeys && data.foreignKeys.length > 0) {
            html += `
                <div class="details-section">
                    <h3><i class="fa-solid fa-link"></i> Relationships (Foreign Keys)</h3>
                    <div class="schema-table-wrapper">
                        <table class="schema-table">
                            <thead>
                                <tr>
                                    <th>Constrained Columns</th>
                                    <th>Referenced Table</th>
                                    <th>Referenced Columns</th>
                                </tr>
                            </thead>
                            <tbody>
            `;

            data.foreignKeys.forEach(fk => {
                html += `
                    <tr>
                        <td style="font-family: var(--font-mono); font-size: 12px;">${fk.constrained_columns.join(', ')}</td>
                        <td style="font-weight: 600; color: var(--primary);"><i class="fa-solid fa-table"></i> ${fk.referred_table}</td>
                        <td style="font-family: var(--font-mono); font-size: 12px;">${fk.referred_columns.join(', ')}</td>
                    </tr>
                `;
            });

            html += `
                            </tbody>
                        </table>
                    </div>
                </div>
            `;
        }

        // Section 3: Data Preview Container
        html += `
            <div class="details-section" id="data-preview-section">
                <!-- Data will be loaded dynamically here -->
            </div>
        `;

        tableDetailsContainer.innerHTML = html;
    }

    // Load table details (schema + 1st page of data), setting up cache & prefetch task
    async function loadTableDetails(tableName) {
        // If table is cached, render instantly without fetching or loading screens
        if (tableCache[tableName]) {
            const cache = tableCache[tableName];
            renderTableLayout(cache);
            renderTableData(tableName, cache.columns, cache.rows[1], {
                page: 1,
                page_size: 50,
                total_rows: cache.totalRows
            });
            // Prefetch pages 2 to 10 for the active table in parallel if not done yet
            if (cache.prefetchedUntilPage === 1) {
                prefetchTablePages(tableName, 2, 10);
            }
            return;
        }

        tableDetailsContainer.innerHTML = '<div class="empty-explorer-state"><i class="fa-solid fa-spinner fa-spin"></i><h2>Loading Details...</h2><p>Fetching schemas and generating data preview grids...</p></div>';

        try {
            const response = await authenticatedFetch(`/api/tables/${tableName}?page=1&page_size=50`);
            if (!response.ok) throw new Error('Failed to load table details');
            const data = await response.json();

            // Populate cache structure
            tableCache[tableName] = {
                name: data.name,
                columns: data.columns,
                foreignKeys: data.foreign_keys,
                totalRows: data.pagination.total_rows,
                rows: {
                    1: data.preview_data
                },
                prefetchedUntilPage: 1
            };

            renderTableLayout(tableCache[tableName]);
            renderTableData(tableName, data.columns, data.preview_data, data.pagination);

            // Fetch pages 2 to 10 in the background asynchronously
            prefetchTablePages(tableName, 2, 10);

        } catch (error) {
            tableDetailsContainer.innerHTML = `
                <div class="empty-explorer-state" style="color: var(--error);">
                    <i class="fa-solid fa-triangle-exclamation"></i>
                    <h2>Failed to Load Details</h2>
                    <p>${error.message}</p>
                </div>
            `;
        }
    }

    // Fetches page data from cache or falls back to server-side fetch
    async function loadTablePageData(tableName, page, columns) {
        const previewSection = document.getElementById('data-preview-section');
        if (!previewSection) return;

        const cache = tableCache[tableName];
        
        // 1. Cache Hit: Render instantly (0ms delay)
        if (cache && cache.rows[page]) {
            renderTableData(tableName, columns, cache.rows[page], {
                page: page,
                page_size: 50,
                total_rows: cache.totalRows
            });
            triggerConditionalPrefetch(tableName, page);
            return;
        }

        // 2. Cache Miss: Fetch from backend API
        previewSection.innerHTML = `
            <h3><i class="fa-solid fa-table-cells"></i> Live Data Preview</h3>
            <div style="text-align: center; padding: 40px; color: var(--text-muted);">
                <i class="fa-solid fa-spinner fa-spin" style="font-size: 24px; margin-bottom: 8px;"></i>
                <p>Loading page data...</p>
            </div>
        `;

        try {
            const response = await authenticatedFetch(`/api/tables/${tableName}/data?page=${page}&page_size=50`);
            if (!response.ok) throw new Error('Failed to load table page data');
            const data = await response.json();

            if (cache) {
                cache.rows[page] = data.preview_data;
            }

            renderTableData(tableName, columns, data.preview_data, data.pagination);
            triggerConditionalPrefetch(tableName, page);
        } catch (error) {
            previewSection.innerHTML = `
                <div style="color: var(--error); padding: 20px; border: 1px solid var(--border-color); border-radius: 8px;">
                    <i class="fa-solid fa-triangle-exclamation"></i> Failed to load page data: ${error.message}
                </div>
            `;
        }
    }

    // Renders the data table & pagination controls inside the container
    function renderTableData(tableName, columns, preview_data, pagination) {
        const previewSection = document.getElementById('data-preview-section');
        if (!previewSection) return;

        const totalPages = Math.ceil(pagination.total_rows / pagination.page_size);
        const startRow = (pagination.page - 1) * pagination.page_size + 1;
        const endRow = Math.min(pagination.page * pagination.page_size, pagination.total_rows);

        let html = `
            <h3><i class="fa-solid fa-table-cells"></i> Live Data Preview <span style="font-size: 12px; color: var(--text-muted); font-weight: normal; margin-left: 8px;">(Showing ${startRow.toLocaleString()} - ${endRow.toLocaleString()} of ${pagination.total_rows.toLocaleString()} rows)</span></h3>
            <div class="data-table-wrapper">
                <table class="data-table">
                    <thead>
                        <tr>
        `;

        // Column Headers
        const colNames = columns.map(c => c.name);
        colNames.forEach(name => {
            html += `<th>${name}</th>`;
        });

        html += `
                        </tr>
                    </thead>
                    <tbody>
        `;

        // Rows
        preview_data.forEach(row => {
            html += `<tr>`;
            colNames.forEach(colName => {
                const val = row[colName] !== null ? escapeHTML(row[colName]) : '<em style="color:#94a3b8;">NULL</em>';
                html += `<td>${val}</td>`;
            });
            html += `</tr>`;
        });

        html += `
                    </tbody>
                </table>
            </div>

            <!-- Pagination Controls -->
            <div class="pagination-controls">
                <button class="pag-btn" id="prev-page-btn" ${pagination.page <= 1 ? 'disabled' : ''}>
                    <i class="fa-solid fa-chevron-left"></i> Prev
                </button>
                <span style="font-weight: 600; font-size: 13px; color: var(--text-main);">
                    Page ${pagination.page} of ${totalPages}
                </span>
                <button class="pag-btn" id="next-page-btn" ${pagination.page >= totalPages ? 'disabled' : ''}>
                    Next <i class="fa-solid fa-chevron-right"></i>
                </button>
            </div>
        `;

        previewSection.innerHTML = html;

        // Bind event listeners to buttons
        const prevPageBtn = document.getElementById('prev-page-btn');
        const nextPageBtn = document.getElementById('next-page-btn');

        if (prevPageBtn) {
            prevPageBtn.addEventListener('click', () => {
                loadTablePageData(tableName, pagination.page - 1, columns);
            });
        }
        if (nextPageBtn) {
            nextPageBtn.addEventListener('click', () => {
                loadTablePageData(tableName, pagination.page + 1, columns);
            });
        }
    }

    // ----------------------------------------
    // HELPERS
    // ----------------------------------------
    
    // Escapes raw HTML tags to prevent XSS
    function escapeHTML(str) {
        if (str === null || str === undefined) return '';
        return String(str).replace(/[&<>'"]/g, 
            tag => ({
                '&': '&amp;',
                '<': '&lt;',
                '>': '&gt;',
                "'": '&#39;',
                '"': '&quot;'
            }[tag] || tag)
        );
    }

    // Helper to format inline tags (bold, italic, code)
    function formatInline(text) {
        let html = escapeHTML(text);
        html = html.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
        html = html.replace(/\*(.*?)\*/g, '<em>$1</em>');
        html = html.replace(/`(.*?)`/g, '<code style="font-family:var(--font-mono); font-size:12px; background:#f1f5f9; padding:2px 4px; border-radius:4px; border:1px solid #e2e8f0; color:#db2777;">$1</code>');
        return html;
    }

    // Advanced Markdown parser supporting headers, lists, code, bold, and dynamic HTML tables
    function formatMarkdown(text) {
        if (text === null || text === undefined) return '';
        
        let lines = text.split('\n');
        let resultHtml = [];
        let inTable = false;
        let tableHeaders = [];
        let tableRows = [];
        let inUl = false;
        let inOl = false;
        
        function flushTable() {
            if (inTable) {
                let tableHtml = '<div class="data-table-wrapper" style="margin:12px 0; max-width:100%; overflow-x:auto;"><table class="data-table">';
                
                // Headers
                tableHtml += '<thead><tr>';
                tableHeaders.forEach(h => {
                    tableHtml += `<th>${formatInline(h)}</th>`;
                });
                tableHtml += '</tr></thead>';
                
                // Rows
                tableHtml += '<tbody>';
                tableRows.forEach(row => {
                    tableHtml += '<tr>';
                    // Pad row cells if there are fewer cells than headers
                    for (let c = 0; c < tableHeaders.length; c++) {
                        let cellVal = row[c] !== undefined ? row[c] : '';
                        tableHtml += `<td>${formatInline(cellVal)}</td>`;
                    }
                    tableHtml += '</tr>';
                });
                tableHtml += '</tbody></table></div>';
                
                resultHtml.push(tableHtml);
                inTable = false;
                tableHeaders = [];
                tableRows = [];
            }
        }
        
        function flushUl() {
            if (inUl) {
                resultHtml.push('</ul>');
                inUl = false;
            }
        }
        
        function flushOl() {
            if (inOl) {
                resultHtml.push('</ol>');
                inOl = false;
            }
        }
        
        for (let i = 0; i < lines.length; i++) {
            let line = lines[i].trim();
            
            // Check if line looks like a table row: starts and ends with |
            if (line.startsWith('|') && line.endsWith('|')) {
                flushUl();
                flushOl();
                
                let cells = line.split('|').map(c => c.trim());
                // Remove boundaries
                if (cells[0] === '') cells.shift();
                if (cells[cells.length - 1] === '') cells.pop();
                
                // Check if it is a separator: |---|---|
                let isSeparator = cells.every(c => c.match(/^:?-+:?$/));
                if (isSeparator) {
                    continue; // Skip separator line
                }
                
                if (!inTable) {
                    inTable = true;
                    tableHeaders = cells;
                } else {
                    tableRows.push(cells);
                }
            } else {
                flushTable();
                
                // Check for headers (e.g. #, ##, ###, ####, #####, ######)
                let headerMatch = line.match(/^(#{1,6})\s+(.*)$/);
                if (headerMatch) {
                    flushUl();
                    flushOl();
                    let level = headerMatch[1].length;
                    let headingText = headerMatch[2];
                    const levelSizes = { 1: '1.6em', 2: '1.4em', 3: '1.2em', 4: '1.1em', 5: '1em', 6: '0.9em' };
                    let fontSize = levelSizes[level] || '1.1em';
                    resultHtml.push(`<h${level} style="margin-top: 14px; margin-bottom: 6px; font-family: var(--font-display); font-weight: 700; color: var(--text-main); font-size: ${fontSize};">${formatInline(headingText)}</h${level}>`);
                    continue;
                }
                
                // Check for unordered list item (- or *)
                let ulMatch = line.match(/^[\-\*]\s+(.*)$/);
                if (ulMatch) {
                    flushOl();
                    if (!inUl) {
                        resultHtml.push('<ul style="margin: 6px 0; padding-left: 20px; list-style-type: disc;">');
                        inUl = true;
                    }
                    resultHtml.push(`<li style="margin: 3px 0; line-height: 1.5;">${formatInline(ulMatch[1])}</li>`);
                    continue;
                }
                
                // Check for ordered list item (e.g. 1.)
                let olMatch = line.match(/^(\d+)\.\s+(.*)$/);
                if (olMatch) {
                    flushUl();
                    if (!inOl) {
                        resultHtml.push('<ol style="margin: 6px 0; padding-left: 20px; list-style-type: decimal;">');
                        inOl = true;
                    }
                    resultHtml.push(`<li style="margin: 3px 0; line-height: 1.5;">${formatInline(olMatch[2])}</li>`);
                    continue;
                }
                
                // Empty lines
                if (line === '') {
                    flushUl();
                    flushOl();
                    resultHtml.push('<br>');
                    continue;
                }
                
                // Normal paragraph line
                flushUl();
                flushOl();
                resultHtml.push(`<div style="margin: 4px 0; line-height: 1.5;">${formatInline(lines[i])}</div>`);
            }
        }
        flushTable();
        flushUl();
        flushOl();
        
        return resultHtml.join('');
    }

    // Bind custom SQL Trigger click listener
    if (sqlEditorTriggerBtn) {
        sqlEditorTriggerBtn.addEventListener('click', () => {
            // Remove active class from all table items
            document.querySelectorAll('.table-item').forEach(item => item.classList.remove('active'));
            
            // Mark trigger button as active
            sqlEditorTriggerBtn.classList.add('active');
            
            // Render custom SQL editor UI
            renderSqlEditor();
        });
    }

    // Render the Custom SQL Query UI
    function renderSqlEditor() {
        tableDetailsContainer.innerHTML = `
            <div class="sql-editor-container">
                <div class="table-details-header">
                    <div class="table-title">
                        <h2><i class="fa-solid fa-terminal"></i> Custom SQL Query Runner</h2>
                        <p>Write and execute read-only PostgreSQL SELECT queries against AWS RDS</p>
                    </div>
                </div>
                
                <div class="sql-textarea-wrapper">
                    <textarea class="sql-textarea" id="custom-sql-input" placeholder="SELECT * FROM orders LIMIT 10;"></textarea>
                </div>
                
                <div class="sql-controls">
                    <span style="font-size: 12px; color: var(--text-muted); font-weight: 500;">
                        <i class="fa-solid fa-shield-halved"></i> Read-only queries only (SELECT or WITH)
                    </span>
                    <button class="sql-run-btn" id="custom-sql-run-btn">
                        <i class="fa-solid fa-play"></i> Run Query
                    </button>
                </div>
                
                <div id="sql-results-container" style="margin-top: 20px;"></div>
            </div>
        `;
        
        const runBtn = document.getElementById('custom-sql-run-btn');
        const sqlInput = document.getElementById('custom-sql-input');
        const resultsContainer = document.getElementById('sql-results-container');
        
        console.log("[SQL Editor] Rendered elements:", { runBtn, sqlInput, resultsContainer });
        
        if (runBtn && sqlInput && resultsContainer) {
            console.log("[SQL Editor] Registering click listener for runBtn");
            runBtn.addEventListener('click', async (e) => {
                e.preventDefault();
                console.log("[SQL Editor] runBtn clicked!");
                
                const sqlQuery = sqlInput.value.trim();
                console.log("[SQL Editor] SQL Query typed:", sqlQuery);
                if (!sqlQuery) {
                    console.log("[SQL Editor] SQL Query is empty, ignoring click.");
                    return;
                }
                
                runBtn.disabled = true;
                runBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Executing...';
                resultsContainer.innerHTML = `
                    <div style="text-align: center; padding: 40px; color: var(--text-muted);">
                        <i class="fa-solid fa-spinner fa-spin" style="font-size: 24px; margin-bottom: 8px;"></i>
                        <p>Querying AWS RDS PostgreSQL...</p>
                    </div>
                `;
                
                try {
                    const response = await authenticatedFetch('/api/query', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ sql: sqlQuery })
                    });
                    
                    const result = await response.json();
                    if (!response.ok) {
                        throw new Error(result.detail || 'Failed to execute query');
                    }
                    
                    renderCustomQueryResult(result.data, resultsContainer);
                } catch (err) {
                    resultsContainer.innerHTML = `
                        <div class="sql-error-box">
                            <i class="fa-solid fa-triangle-exclamation"></i> <strong>Database Error:</strong>
                            <pre style="margin-top: 8px; font-family: var(--font-mono); white-space: pre-wrap; font-size: 12px; margin-bottom: 0;">${err.message}</pre>
                        </div>
                    `;
                } finally {
                    runBtn.disabled = false;
                    runBtn.innerHTML = '<i class="fa-solid fa-play"></i> Run Query';
                }
            });
        }
    }

    // Render Custom SQL Query data table
    function renderCustomQueryResult(data, container) {
        if (!data || data.length === 0) {
            container.innerHTML = `
                <div style="padding: 20px; text-align: center; border: 1px solid var(--border-color); border-radius: var(--radius-sm); color: var(--text-muted); font-size: 13px;">
                    <i class="fa-solid fa-circle-info"></i> Query returned 0 rows successfully.
                </div>
            `;
            return;
        }
        
        // Extract warning if present
        let warningMessage = '';
        const warningIdx = data.findIndex(row => row._warning !== undefined);
        if (warningIdx !== -1) {
            warningMessage = data[warningIdx]._warning;
            data.splice(warningIdx, 1);
        }
        
        if (data.length === 0) {
            container.innerHTML = `
                <div style="padding: 20px; text-align: center; border: 1px solid var(--border-color); border-radius: var(--radius-sm); color: var(--text-muted); font-size: 13px;">
                    <i class="fa-solid fa-circle-info"></i> ${warningMessage}
                </div>
            `;
            return;
        }
        
        const columns = Object.keys(data[0]);
        
        let html = `
            <h3 style="margin-bottom: 10px; font-size: 14px; font-weight: 700;"><i class="fa-solid fa-list-ol"></i> Query Results <span style="font-size: 12px; color: var(--text-muted); font-weight: normal; margin-left: 8px;">(Showing ${data.length} rows)</span></h3>
        `;
        
        if (warningMessage) {
            html += `
                <div style="background: #fffbeb; border: 1px solid #fef3c7; color: #b45309; padding: 10px 14px; border-radius: 6px; font-size: 12px; margin-bottom: 12px;">
                    <i class="fa-solid fa-circle-info"></i> ${warningMessage}
                </div>
            `;
        }
        
        html += `
            <div class="data-table-wrapper" style="max-height: 350px;">
                <table class="data-table">
                    <thead>
                        <tr>
        `;
        
        columns.forEach(col => {
            html += `<th>${col}</th>`;
        });
        
        html += `
                        </tr>
                    </thead>
                    <tbody>
        `;
        
        data.forEach(row => {
            html += '<tr>';
            columns.forEach(col => {
                const cellVal = row[col] !== null && row[col] !== undefined ? row[col] : 'NULL';
                html += `<td>${escapeHTML(cellVal)}</td>`;
            });
            html += '</tr>';
        });
        
        html += `
                    </tbody>
                </table>
            </div>
        `;
        
        container.innerHTML = html;
    }

    // Bind explorer sidebar header click listener to return to schema guide
    if (explorerSidebarTitle) {
        explorerSidebarTitle.addEventListener('click', () => {
            // Remove active states from table items and query trigger
            document.querySelectorAll('.table-item').forEach(item => item.classList.remove('active'));
            if (sqlEditorTriggerBtn) sqlEditorTriggerBtn.classList.remove('active');
            
            // Render guide
            renderSchemaGuide();
        });
    }

    // Render the Olist Database Schema Guide UI
    function renderSchemaGuide() {
        tableDetailsContainer.innerHTML = `
            <div class="schema-guide-container">
                <div class="guide-header">
                    <h2>Olist Database Schema Guide</h2>
                    <p>This dashboard is connected to the real, public <a href="https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce" target="_blank" class="kaggle-link">Olist Brazilian E-Commerce Dataset</a> on Kaggle.</p>
                </div>
                
                <div class="schema-map-card">
                    <h3>Database Relationship Map (ERD)</h3>
                    <div style="text-align: center; padding: 12px; background: #ffffff; border: 1px solid var(--border-color); border-radius: var(--radius-md); overflow: hidden; display: flex; justify-content: center; align-items: center;">
                        <img src="erd.png" alt="Olist DB ERD Schema Map" style="max-width: 100%; height: auto; border-radius: var(--radius-sm); border: 1px solid #f1f5f9; display: block;">
                    </div>
                </div>

                <div class="guide-tips-grid">
                    <div class="tip-card">
                        <h4>customer_id vs customer_unique_id</h4>
                        <p><strong>customer_id:</strong> A temporary key generated for each order session. Use this to JOIN <code>orders</code> and <code>customers</code>.</p>
                        <p><strong>customer_unique_id:</strong> The actual identifier of a physical customer. Use this to count unique buyers or track repeat purchases.</p>
                    </div>
                    <div class="tip-card">
                        <h4>order_purchase_timestamp vs shipping_limit_date</h4>
                        <p><strong>order_purchase_timestamp:</strong> The actual date and time when the order occurred. Use this for all sales, freight, or order volume date-filtering.</p>
                        <p><strong>shipping_limit_date:</strong> The deadline/limit for the seller to ship the product. Do NOT use this as order date filters.</p>
                    </div>
                </div>
            </div>
        `;
    }
});
