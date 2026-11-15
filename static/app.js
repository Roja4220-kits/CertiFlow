(() => {
    "use strict";

    // ------------------------------------------------------------------
    // DOM
    // ------------------------------------------------------------------
    const $ = (id) => document.getElementById(id);
    const IDS = [
        "templateFile", "participantFile", "templateInfo", "participantInfo",
        "templatePreview", "templateImage", "templatePdf", "pdfNotice", "templateType",
        "participantPreview", "participantCount", "previewMessage", "tableNote",
        "participantTableHead", "participantTableBody",
        "validationPanel", "validationStats", "validationMessages",
        "positionSettingsPanel", "dynamicPositionFields", "templateFields",
        "fontUploadInput", "fontUploadButton", "fontList",
        "previewButton", "generateButton", "generateHint", "sampleButton",
        "skipInvalidWrap", "skipInvalid", "skipInvalidLabel",
        "progressWrap", "progressBar", "progressText",
        "samplePreviewButton", "samplePreviewPanel", "samplePreviewFrame",
        "statusMessage", "resultPanel", "resultMessage",
        "totalCount", "successCount", "failedCount", "skippedNote",
        "jobStatusArea", "jobIdText", "jobStatusLink", "individualResults",
    ];
    const el = Object.fromEntries(IDS.map((id) => [id, $(id)]));

    // ------------------------------------------------------------------
    // State
    // ------------------------------------------------------------------
    let previewData = null;   // response of /api/preview-upload
    let report = null;        // latest validation report
    let fonts = [];           // custom fonts: [{id, label}]
    let templateObjectUrl = null;
    let busy = false;
    let validateTimer = null;
    let validateSeq = 0;
    let recheckAfterReupload = false;
    let labelColumn = "";     // column used to name participants in messages

    const FONT_MAP = {
        sans:  { regular: "helv", bold: "hebo", italic: "heit", bold_italic: "hebi" },
        serif: { regular: "tiro", bold: "tibo", italic: "tiit", bold_italic: "tibi" },
        mono:  { regular: "cour", bold: "cobo", italic: "coit", bold_italic: "cobi" },
    };

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------
    /** Build a DOM node. Text is always inserted as text (never as HTML). */
    function h(tag, attrs = {}, ...children) {
        const node = document.createElement(tag);
        for (const [key, value] of Object.entries(attrs)) {
            if (value == null || value === false) continue;
            if (key === "class") node.className = value;
            else if (key === "text") node.textContent = value;
            else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
            else node.setAttribute(key, value === true ? "" : value);
        }
        for (const child of children.flat()) {
            if (child != null && child !== false) node.append(child);
        }
        return node;
    }

    const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => (
        { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
    ));

    // Must match normalize_field_name() in fieldnames.py
    function normalize(value) {
        return String(value ?? "").trim().toLowerCase()
            .replace(/[\s-]+/g, "_")
            .replace(/[^\p{L}\p{N}\p{M}_]/gu, "")
            .replace(/_+/g, "_")
            .replace(/^_|_$/g, "");
    }

    const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
    const humanize = (field) => {
        const text = String(field).replace(/_/g, " ").trim();
        return text.charAt(0).toUpperCase() + text.slice(1);
    };
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

    function setStatus(message, kind = "info") {
        el.statusMessage.textContent = message;
        const banner = $("statusBanner");
        if (banner) banner.dataset.state = kind;
    }

    function setStep(stepNumber) {
        document.querySelectorAll(".step").forEach((step) => {
            const current = Number(step.dataset.step);
            step.classList.toggle("active", current === stepNumber);
            step.classList.toggle("completed", current < stepNumber);
        });
    }

    function revokeSamplePreview() {
        const previous = el.samplePreviewFrame.dataset.objectUrl;
        if (previous) {
            URL.revokeObjectURL(previous);
            delete el.samplePreviewFrame.dataset.objectUrl;
        }
        el.samplePreviewFrame.removeAttribute("src");
    }

    async function readJson(response) {
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const error = new Error(data.error || `Request failed (${response.status}).`);
            error.data = data;
            throw error;
        }
        return data;
    }

    // ------------------------------------------------------------------
    // Reset
    // ------------------------------------------------------------------
    function clearPreviewState() {
        previewData = null;
        report = null;
        fonts = [];
        labelColumn = "";
        clearTimeout(validateTimer);
        validateSeq++;

        for (const id of ["positionSettingsPanel", "participantPreview", "validationPanel",
            "generateButton", "resultPanel", "samplePreviewPanel", "progressWrap", "skipInvalidWrap"]) {
            el[id].classList.add("hidden");
        }
        el.generateButton.disabled = true;
        el.skipInvalid.checked = false;
        el.previewButton.classList.remove("hidden");
        el.previewButton.disabled = !(el.templateFile.files.length && el.participantFile.files.length);
        el.generateHint.textContent = "Upload both files, then click “Check my files”.";

        for (const id of ["dynamicPositionFields", "templateFields", "participantTableHead",
            "participantTableBody", "validationStats", "validationMessages", "fontList"]) {
            el[id].replaceChildren();
        }
        revokeSamplePreview();
        setStep(1);
    }

    function showSelectedFile(input, info, label) {
        const file = input.files[0];
        if (!file) { info.classList.add("hidden"); return; }
        info.textContent = `${label}: ${file.name} (${(file.size / 1024 / 1024).toFixed(2)} MB)`;
        info.classList.remove("hidden");
    }

    function showTemplatePreview(file) {
        if (templateObjectUrl) URL.revokeObjectURL(templateObjectUrl);
        templateObjectUrl = URL.createObjectURL(file);
        el.templatePreview.classList.remove("hidden");
        el.templateType.textContent = file.name.split(".").pop().toUpperCase();
        const isPdf = file.name.toLowerCase().endsWith(".pdf");
        el.templateImage.classList.toggle("hidden", isPdf);
        el.templatePdf.classList.toggle("hidden", !isPdf);
        el.pdfNotice.classList.toggle("hidden", !isPdf);
        (isPdf ? el.templatePdf : el.templateImage).src = templateObjectUrl;
    }

    // ------------------------------------------------------------------
    // Field cards (mapping + formatting)
    // ------------------------------------------------------------------
    const getCards = () => [...el.dynamicPositionFields.querySelectorAll(".dynamic-position-card")];
    const cardOf = (field) => getCards().find((card) => card.dataset.field === field);

    function currentMapping() {
        const mapping = {};
        for (const card of getCards()) mapping[card.dataset.field] = card.querySelector(".field-source").value;
        return mapping;
    }

    function optionalFields() {
        return getCards()
            .filter((card) => card.querySelector(".field-optional-box").checked)
            .map((card) => card.dataset.field);
    }

    function uniqueFields() {
        return getCards().filter((c) => c.querySelector(".field-unique-box").checked).map((c) => c.dataset.field);
    }

    function fieldFormats() {
        const formats = {};
        for (const card of getCards()) formats[card.dataset.field] = card.querySelector(".field-format").value;
        return formats;
    }

    /** Everything the user chose that affects validation (sent to /api/validate and /api/certificates/bulk). */
    function currentChecks() {
        return {
            mapping: currentMapping(),
            optional_fields: optionalFields(),
            unique_fields: uniqueFields(),
            field_formats: fieldFormats(),
            label_column: labelColumn || report?.label_column || "",
        };
    }

    function headerLabel(column) {
        return (previewData?.original_headers || {})[column] || column;
    }

    function fontOptions(selected) {
        const base = [["sans", "Sans serif"], ["serif", "Serif"], ["mono", "Monospace"]];
        const custom = fonts.map((font) => [`font:${font.id}`, font.label]);
        const option = (value, label) => `<option value="${escapeHtml(value)}"${value === selected ? " selected" : ""}>${escapeHtml(label)}</option>`;
        return base.map(([v, l]) => option(v, l)).join("") +
            (custom.length ? `<optgroup label="Your fonts">${custom.map(([v, l]) => option(v, l)).join("")}</optgroup>` : "");
    }

    function refreshFontSelects() {
        for (const card of getCards()) {
            const select = card.querySelector(".field-family");
            const previous = select.value;
            select.innerHTML = fontOptions(previous);
            if (![...select.options].some((o) => o.value === previous)) select.value = "sans";
            card.querySelector(".field-style").disabled = select.value.startsWith("font:");
        }
    }

    function createFieldCard(field, data, medianSize = 0) {
        const available = data.available_fields || [];
        const match = available.find((column) => normalize(column) === normalize(field)) || "";
        const auto = (data.automatic_positions || {})[field] || {};
        const size = auto.font_size
            ? Math.min(100, Math.max(6, Math.round(auto.font_size)))
            : 20;
        const color = /^#[0-9a-fA-F]{6}$/.test(auto.color || "") ? auto.color : "#000000";
        // Bold only for text that is clearly larger than the rest of the template (no name guessing).
        const bold = Boolean(auto.font_size && medianSize && auto.font_size >= medianSize * 1.25);

        const columnOptions =
            `<option value=""${match ? "" : " selected"}>— not in my file —</option>` +
            available.map((column) =>
                `<option value="${escapeHtml(column)}"${column === match ? " selected" : ""}>${escapeHtml(headerLabel(column))}</option>`
            ).join("");

        const manual = data.requires_positions ? `
            <label>X (pt from left)<input class="field-x" type="number" value="400" step="1"></label>
            <label>Y (pt from bottom)<input class="field-y" type="number" value="300" step="1"></label>` : "";

        const card = h("article", { class: "dynamic-position-card", "data-field": field });
        card.innerHTML = `
            <div class="card-head">
                <code class="ph-name">{{${escapeHtml(field)}}}</code>
                <span class="mini-tag field-status"></span>
            </div>
            <div class="field-hint"></div>

            <div class="card-section">
                <p class="card-label">1 · Where does this text come from?</p>
                <select class="field-source" aria-label="Column for ${escapeHtml(field)}">${columnOptions}</select>
                <div class="rule-row">
                    <label class="chip-check"><input class="field-optional-box" type="checkbox"> Can be empty</label>
                    <label class="chip-check"><input class="field-unique-box" type="checkbox"> Must be unique</label>
                    <label class="select-inline">Format
                        <select class="field-format">
                            <option value="any">Any text</option>
                            <option value="date">Date</option>
                            <option value="number">Number</option>
                            <option value="email">Email</option>
                        </select>
                    </label>
                </div>
            </div>

            <div class="card-section">
                <p class="card-label">2 · How should it look?</p>
                <div class="style-grid">
                    <label>Size
                        <input class="field-size" type="number" value="${size}" min="6" max="100" step="1">
                    </label>
                    <label>Alignment
                        <select class="field-align">
                            <option value="center" selected>Center</option>
                            <option value="left">Left</option>
                            <option value="right">Right</option>
                        </select>
                    </label>
                    <label>Font
                        <select class="field-family">${fontOptions("sans")}</select>
                    </label>
                    <label>Style
                        <select class="field-style">
                            <option value="regular">Regular</option>
                            <option value="bold"${bold ? " selected" : ""}>Bold</option>
                            <option value="italic">Italic</option>
                            <option value="bold_italic">Bold italic</option>
                        </select>
                    </label>
                    <label>Color
                        <input class="field-color" type="color" value="${color}">
                    </label>
                    ${manual}
                </div>
            </div>`;

        card.querySelector(".field-source").addEventListener("change", scheduleValidation);
        card.querySelector(".field-optional-box").addEventListener("change", scheduleValidation);
        card.querySelector(".field-unique-box").addEventListener("change", scheduleValidation);
        card.querySelector(".field-format").addEventListener("change", scheduleValidation);
        card.querySelector(".field-family").addEventListener("change", (event) => {
            card.querySelector(".field-style").disabled = event.target.value.startsWith("font:");
        });
        el.dynamicPositionFields.appendChild(card);
    }

    function buildFieldSettings(data) {
        el.dynamicPositionFields.replaceChildren();
        el.templateFields.replaceChildren();
        const fields = data.template_fields || [];
        if (!fields.length) {
            throw new Error("No placeholders were found in your template. See “Prepare your files” for how to write them.");
        }
        const sizes = fields.map((f) => (data.automatic_positions || {})[f]?.font_size).filter(Boolean).sort((a, b) => a - b);
        const median = sizes.length ? sizes[Math.floor(sizes.length / 2)] : 0;
        for (const field of fields) {
            el.templateFields.appendChild(h("span", { text: `{{${field}}}` }));
            createFieldCard(field, data, median);
        }
        el.positionSettingsPanel.classList.remove("hidden");
    }

    /** Buttons that let the user confirm a suggested column (a suggestion is never applied silently). */
    function suggestionNodes(field) {
        const suggestion = report?.suggestions?.[field];
        if (!suggestion) return [];
        return [
            h("span", { class: "hint-text", text: suggestion.status === "suggested" ? "Possible match:" : "Which column is it?" }),
            ...suggestion.candidates.map((c) => h("button", {
                type: "button", class: "hint-button",
                onclick: () => setMapping(field, c.column), text: `Use “${headerLabel(c.column)}”`,
            })),
        ];
    }

    function updateCardStates() {
        if (!report) return;
        for (const card of getCards()) {
            const field = card.dataset.field;
            const tag = card.querySelector(".field-status");
            const column = report.column_for?.[field];
            const missing = report.missing_columns.includes(field);
            const optional = card.querySelector(".field-optional-box").checked;
            card.classList.toggle("has-error", missing);
            tag.className = "mini-tag field-status";
            if (missing) { tag.textContent = "Column missing"; tag.classList.add("tag-bad"); }
            else if (!column) { tag.textContent = "Optional · left blank"; tag.classList.add("tag-opt"); }
            else if (optional) { tag.textContent = "Optional"; tag.classList.add("tag-opt"); }
            else { tag.textContent = "Matched"; tag.classList.add("tag-ok"); }
            card.querySelector(".field-hint").replaceChildren(...(column ? [] : suggestionNodes(field)));
        }
    }

    function getPositions() {
        const positions = {};
        for (const card of getCards()) {
            const field = card.dataset.field;
            const family = card.querySelector(".field-family").value;
            const style = card.querySelector(".field-style").value;
            const fontSize = Number(card.querySelector(".field-size").value);
            if (!Number.isFinite(fontSize) || fontSize < 6 || fontSize > 100) {
                throw new Error(`Font size for {{${field}}} must be between 6 and 100.`);
            }
            positions[field] = {
                font_size: fontSize,
                align: card.querySelector(".field-align").value,
                font_name: family.startsWith("font:") ? family.slice(5) : FONT_MAP[family][style],
                color: card.querySelector(".field-color").value,
            };
            const xInput = card.querySelector(".field-x");
            const yInput = card.querySelector(".field-y");
            if (xInput && yInput) {
                const x = Number(xInput.value), y = Number(yInput.value);
                if (!Number.isFinite(x) || !Number.isFinite(y)) throw new Error(`Enter numeric X and Y positions for {{${field}}}.`);
                Object.assign(positions[field], { x, y, use_template_position: false });
            }
        }
        return positions;
    }

    // ------------------------------------------------------------------
    // Validation (server does the checking; this renders the result)
    // ------------------------------------------------------------------
    function scheduleValidation() {
        if (!previewData) return;
        clearTimeout(validateTimer);
        validateTimer = setTimeout(runValidation, 200);
    }

    async function runValidation() {
        const seq = ++validateSeq;
        try {
            const response = await fetch("/api/validate", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    upload_id: previewData.upload_id,
                    template_fields: previewData.template_fields,
                    ...currentChecks(),
                }),
            });
            const data = await readJson(response);
            if (seq !== validateSeq) return;  // a newer check is already running
            report = data.report;
            renderAll();
        } catch (error) {
            setStatus(error.message, "error");
        }
    }

    function statCard(label, value, tone) {
        return h("div", { class: `v-stat ${tone || ""}` }, h("span", { text: label }), h("strong", { text: String(value) }));
    }

    function issueDetail(issue) {
        const row = (label, value) => h("tr", {}, h("th", { text: label }), h("td", { text: String(value) }));
        return h("div", { class: "v-table-wrap" }, h("table", {}, h("tbody", {},
            row("Row", `${issue.row} (counting the header row)`),
            row("Participant", issue.participant),
            row("Missing field", issue.field),
            row("Problem", issue.problem),
        )));
    }

    function dataTable(headers, rows, total) {
        const shown = rows.slice(0, 200);
        return h("div", { class: "v-table-wrap" },
            h("table", {},
                h("thead", {}, h("tr", {}, headers.map((t) => h("th", { text: t })))),
                h("tbody", {}, shown.map((cells) => h("tr", {}, cells.map((v) => h("td", { text: String(v) })))))),
            total > shown.length ? h("div", { class: "v-more", text: `…and ${total - shown.length} more. Fix the ones above first, then check again.` }) : null);
    }

    function reuploadParticipants() {
        recheckAfterReupload = true;
        el.participantFile.value = "";
        el.participantFile.click();
    }

    function buildMessage(message) {
        const icon = { error: "!", info: "i", success: "✓" }[message.level] || "i";
        const body = h("div", {}, h("h3", { text: message.title }), h("p", { text: message.summary }));

        if (message.items?.length) {
            body.append(
                message.items_label ? h("p", { text: message.items_label }) : null,
                h("div", { class: "v-chips" }, message.items.map((item) => h("span", { class: "v-chip", text: item }))),
            );
        }
        if (message.code === "missing_columns") {
            for (const field of message.fields || []) {
                const nodes = suggestionNodes(field);
                if (nodes.length) body.append(h("div", { class: "v-suggest" }, h("strong", { text: field }), ...nodes));
            }
        }
        if (message.code === "missing_values") {
            const count = message.issue_count ?? message.issues.length;
            body.append(count === 1 ? issueDetail(message.issues[0])
                : dataTable(["Row", "Participant", "Missing field", "Problem"],
                    message.issues.map((i) => [i.row, i.participant, i.field, i.problem]), count));
        }
        if (message.code === "duplicate_values") {
            body.append(dataTable(["Field", "Value", "Rows", "Participants"],
                message.groups.map((g) => [g.field, g.value, g.rows.join(", "), g.participants.join(", ")]), message.group_count));
        }
        if (message.code === "invalid_format") {
            body.append(dataTable(["Row", "Participant", "Field", "Value", "Expected"],
                message.issues.map((i) => [i.row, i.participant, i.field, i.value, i.expected]), message.issue_count));
        }
        if (message.help) body.append(h("div", { class: "v-help", text: message.help }));

        const actions = [];
        const ghost = (text, onclick) => h("button", { type: "button", class: "ghost-button", onclick, text });
        if (message.code === "missing_columns") {
            actions.push(h("button", { type: "button", onclick: () => focusMapping(message.fields), text: "Go to field mapping" }));
            actions.push(ghost("Upload corrected file", reuploadParticipants));
        }
        if (message.code === "missing_values") {
            for (const field of message.fields || []) actions.push(ghost(`Make “${humanize(field)}” optional`, () => setOptional(field, true)));
            actions.push(ghost("Upload corrected file", reuploadParticipants));
        }
        if (message.code === "duplicate_values") {
            for (const field of message.fields || []) actions.push(ghost(`Stop checking “${humanize(field)}” for duplicates`, () => setUnique(field, false)));
            actions.push(ghost("Upload corrected file", reuploadParticipants));
        }
        if (message.code === "invalid_format") {
            for (const field of message.fields || []) actions.push(ghost(`Accept any format for “${humanize(field)}”`, () => setFormat(field, "any")));
            actions.push(ghost("Upload corrected file", reuploadParticipants));
        }
        if (message.code === "unique_hint") {
            for (const field of message.fields || []) actions.push(ghost(`Check “${humanize(field)}” for duplicates`, () => setUnique(field, true)));
        }
        if (actions.length) body.append(h("div", { class: "v-actions" }, actions));

        return h("section", { class: `v-msg v-${message.level}` }, h("div", { class: "v-icon", text: icon }), body);
    }

    /** "Identify participants by" selector (names people in every message). */
    function renderTools() {
        let tools = $("validationTools");
        if (!tools) {
            tools = h("div", { id: "validationTools", class: "v-tools" });
            el.validationStats.before(tools);
        }
        const chosen = labelColumn || report.label_column;
        const select = h("select", {
            id: "labelColumn",
            onchange: (event) => { labelColumn = event.target.value; scheduleValidation(); },
        }, (previewData.available_fields || []).map((c) => h("option", { value: c, text: headerLabel(c), selected: c === chosen })));
        tools.replaceChildren(
            h("label", {}, "Identify participants by ", select),
            h("span", { class: "muted", text: "This column is used to name each person in the messages below." }));
    }

    function renderValidation() {
        if (!report) return;
        const total = report.total_rows;
        const ready = report.missing_columns.length ? 0 : report.valid_rows;
        const errors = report.missing_columns.length + report.problem_count;

        renderTools();
        el.validationStats.replaceChildren(
            statCard("Participants", total),
            statCard("Ready", ready, ready === total && total > 0 ? "good" : ""),
            statCard("Need attention", total - ready, total - ready > 0 ? "bad" : ""),
            statCard("Errors found", errors, errors > 0 ? "bad" : "good"),
            statCard("Blank rows skipped", report.blank_rows_skipped),
            statCard("Ignored columns", report.extra_columns.length),
        );
        el.validationMessages.replaceChildren(...report.messages.map(buildMessage));
        const badge = el.validationPanel.querySelector(".icon-box");
        badge.textContent = errors ? "!" : "✓";
        badge.classList.toggle("icon-err", errors > 0);
        badge.classList.toggle("icon-ok", errors === 0);
        el.validationPanel.classList.remove("hidden");
    }

    function focusMapping(fields) {
        el.positionSettingsPanel.scrollIntoView({ behavior: "smooth", block: "start" });
        let first = null;
        for (const field of fields || []) {
            const card = cardOf(field);
            if (!card) continue;
            card.classList.remove("flash");
            void card.offsetWidth;  // restart the animation
            card.classList.add("flash");
            first = first || card;
        }
        first?.querySelector(".field-source").focus({ preventScroll: true });
    }

    function setMapping(field, column) {
        const card = cardOf(field);
        if (!card) return;
        card.querySelector(".field-source").value = column;
        scheduleValidation();
    }

    function setUnique(field, value) {
        const card = cardOf(field);
        if (!card) return;
        card.querySelector(".field-unique-box").checked = value;
        scheduleValidation();
    }

    function setFormat(field, kind) {
        const card = cardOf(field);
        if (!card) return;
        card.querySelector(".field-format").value = kind;
        scheduleValidation();
    }

    function setOptional(field, value) {
        const card = cardOf(field);
        if (!card) return;
        card.querySelector(".field-optional-box").checked = value;
        scheduleValidation();
    }

    // ------------------------------------------------------------------
    // Participant preview table
    // ------------------------------------------------------------------
    function renderParticipantTable() {
        const columns = previewData.available_fields || [];
        const people = previewData.participants || [];

        // Every problem cell ("row|column") and every note shown beside a row.
        const cellProblems = new Map();
        const rowNotes = new Map();
        const mark = (row, column, kind, text) => {
            const key = `${row}|${column}`;
            if (!cellProblems.has(key)) cellProblems.set(key, { kind, text });
        };
        const note = (row, text) => {
            if (!rowNotes.has(row)) rowNotes.set(row, []);
            rowNotes.get(row).push(text);
        };
        for (const i of report?.cell_issues || []) { mark(i.row, i.column, "empty", "Empty"); note(i.row, i.problem); }
        for (const g of report?.duplicate_groups || []) {
            g.rows.forEach((row, index) => mark(row, g.column, index === 0 ? "first" : "duplicate", index === 0 ? "First use" : "Duplicate"));
        }
        for (const i of report?.duplicate_issues || []) note(i.row, i.problem);
        for (const i of report?.format_issues || []) { mark(i.row, i.column, "format", "Invalid"); note(i.row, `${humanize(i.field)}: ${i.problem}`); }

        const badRows = new Set(report?.invalid_row_numbers || []);
        const blocked = Boolean(report?.missing_columns.length);

        el.participantTableHead.replaceChildren(h("tr", {},
            h("th", { text: "Row" }),
            columns.map((column) => h("th", { text: headerLabel(column) })),
            h("th", { text: "Check" }),
        ));

        const rows = people.map((person) => {
            const state = blocked ? "blocked" : (badRows.has(person.row) ? "error" : "ok");

            const cells = columns.map((column) => {
                const value = person.fields?.[column] ?? "";
                const problem = cellProblems.get(`${person.row}|${column}`);
                if (!problem) return h("td", { text: value });
                const warn = problem.kind === "first";
                return h("td", { class: warn ? "cell-warn" : "cell-error", title: problem.text },
                    h("span", { class: "cell-flag" }, h("i", { text: warn ? "•" : "!" }), problem.text),
                    problem.kind === "empty" ? null : h("span", { class: "cell-value", text: value }));
            });

            const check = state === "ok"
                ? h("span", { class: "validation-badge valid", text: "Ready" })
                : h("div", {},
                    h("span", { class: "validation-badge invalid", text: blocked ? "Blocked" : "Fix this row" }),
                    blocked
                        ? h("span", { class: "row-note", text: `Missing column: ${report.missing_columns.join(", ")}` })
                        : (rowNotes.get(person.row) || []).map((text) => h("span", { class: "row-note", text: `Row ${person.row}: ${text}` })));

            return h("tr", { class: state === "error" ? "row-error" : "" },
                h("td", { text: String(person.row) }), cells, h("td", {}, check));
        });
        el.participantTableBody.replaceChildren(...rows);

        el.participantCount.textContent = `${previewData.total} participants`;
        const used = (report?.matched_fields || []).length;
        el.previewMessage.textContent =
            `${plural(columns.length, "column")} found in your file; ${used} of ` +
            `${plural((previewData.template_fields || []).length, "template field")} matched.`;
        el.tableNote.textContent = previewData.total > people.length
            ? `Showing the first ${people.length} of ${previewData.total} rows. Every row is checked.` : "";
        el.participantPreview.classList.remove("hidden");
    }

    // ------------------------------------------------------------------
    // Generate button / summary
    // ------------------------------------------------------------------
    function updateGenerateAvailability() {
        if (!previewData || !report) { el.generateButton.disabled = true; return; }
        const canSkip = report.can_generate_valid_rows && report.problem_count > 0;
        el.skipInvalidWrap.classList.toggle("hidden", !canSkip);
        if (!canSkip) el.skipInvalid.checked = false;
        if (canSkip) {
            el.skipInvalidLabel.textContent =
                `Skip the ${plural(report.invalid_rows, "participant")} with problems and generate ` +
                `the other ${plural(report.valid_rows, "certificate")}.`;
        }

        const allowed = report.can_generate || (canSkip && el.skipInvalid.checked);
        el.generateButton.disabled = !allowed || busy;

        let message, kind;
        if (report.missing_columns.length) {
            message = `Cannot generate yet: no column for ${report.missing_columns.join(", ")}. Map the field, add the column, or mark it optional.`;
            kind = "error";
        } else if (report.problem_count) {
            message = `${plural(report.problem_count, "problem")} found in ${plural(report.invalid_rows, "row")} ` +
                "(missing, repeated or badly formatted values). Fix your file and upload it again, adjust the field checks, or skip those participants.";
            kind = "error";
        } else {
            message = `All ${plural(report.total_rows, "participant")} are ready. Review the formatting, then generate.`;
            kind = "success";
        }
        el.generateHint.textContent = message;
        if (!busy) setStatus(message, kind);
    }

    function renderAll() {
        renderValidation();
        renderParticipantTable();
        updateCardStates();
        updateGenerateAvailability();
    }

    // ------------------------------------------------------------------
    // Upload
    // ------------------------------------------------------------------
    async function uploadAndPreview() {
        const template = el.templateFile.files[0];
        const participants = el.participantFile.files[0];
        if (!template || !participants) throw new Error("Please select both a certificate template and a participant file.");

        const form = new FormData();
        form.append("template", template);
        form.append("participants", participants);
        const data = await readJson(await fetch("/api/preview-upload", { method: "POST", body: form }));
        if (!data.upload_id) throw new Error("The server did not return an upload ID.");

        previewData = data;
        report = data.report;
        buildFieldSettings(data);
        el.previewButton.classList.add("hidden");
        el.generateButton.classList.remove("hidden");
        el.generateButton.disabled = true;
        renderAll();
        setStep(2);
        loadFonts();

        if (data.ocr_error) setStatus(`Text recognition failed: ${data.ocr_error}`, "warning");
        else if (data.requires_positions) {
            setStatus("No {{placeholders}} were found in the template. Enter X/Y positions for each field (points from the bottom-left corner).", "warning");
        }
    }

    // ------------------------------------------------------------------
    // Fonts
    // ------------------------------------------------------------------
    function renderFontList() {
        el.fontList.replaceChildren(...fonts.map((font) => h("span", { class: "font-pill" }, font.label,
            h("button", { type: "button", class: "secondary-button", text: "Use for all fields", onclick: () => applyFontToAll(font.id) }))));
    }

    function applyFontToAll(fontId) {
        for (const card of getCards()) {
            card.querySelector(".field-family").value = `font:${fontId}`;
            card.querySelector(".field-style").disabled = true;
        }
        setStatus("Font applied to all fields. Use “Preview first participant” to check it.", "success");
    }

    async function loadFonts() {
        try {
            const data = await readJson(await fetch(`/api/fonts?upload_id=${encodeURIComponent(previewData.upload_id)}`));
            fonts = data.fonts || [];
            renderFontList();
            refreshFontSelects();
        } catch (error) { /* fonts are optional: ignore */ }
    }

    async function uploadFont(file) {
        const form = new FormData();
        form.append("upload_id", previewData.upload_id);
        form.append("font", file);
        const font = await readJson(await fetch("/api/fonts", { method: "POST", body: form }));
        fonts.push(font);
        renderFontList();
        refreshFontSelects();
        setStatus(`Font “${font.label}” added. Choose it under “Font family”, or press “Use for all fields”.`, "success");
    }

    // ------------------------------------------------------------------
    // Sample preview
    // ------------------------------------------------------------------
    async function previewFirstParticipant() {
        const bad = new Set(report?.invalid_row_numbers || []);
        const person = (previewData.participants || []).find((p) => !bad.has(p.row));
        if (!person) throw new Error("No ready participant among the preview rows. Fix the data first.");

        const mapping = currentMapping();
        const fields = {};
        for (const [field, column] of Object.entries(mapping)) fields[field] = column ? (person.fields?.[column] ?? "") : "";

        const response = await fetch("/api/preview-certificate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ upload_id: previewData.upload_id, fields, positions: getPositions() }),
        });
        if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            throw new Error(error.error || "Could not create the sample preview.");
        }
        const url = URL.createObjectURL(await response.blob());
        revokeSamplePreview();
        el.samplePreviewFrame.dataset.objectUrl = url;
        el.samplePreviewFrame.src = url;
        el.samplePreviewPanel.classList.remove("hidden");
        setStatus(`Sample preview created for ${person.fields[Object.values(mapping).find(Boolean)] || "the first participant"}.`, "success");
    }

    // ------------------------------------------------------------------
    // Generation (runs in the background on the server; we poll for progress)
    // ------------------------------------------------------------------
    function showProgress(done, total, label) {
        el.progressWrap.classList.remove("hidden");
        el.progressBar.style.width = `${total ? Math.round((done / total) * 100) : 0}%`;
        el.progressText.textContent = label || `Generating certificates… ${done} of ${total}`;
    }

    async function pollJob(job) {
        let failures = 0;
        for (;;) {
            await sleep(1000);
            try {
                const status = await readJson(await fetch(job.status_url, { cache: "no-store" }));
                failures = 0;
                showProgress((status.successful || 0) + (status.failed || 0), status.total);
                if (status.status === "failed") throw new Error(status.error || "The job failed.");
                if (status.status === "completed") {
                    return readJson(await fetch(`${job.status_url}?results=1`, { cache: "no-store" }));
                }
            } catch (error) {
                if (error.message === "Failed to fetch" && ++failures < 6) continue;  // brief network drop
                throw error;
            }
        }
    }

    function showResults(data, skipped) {
        el.resultPanel.classList.remove("hidden");
        el.totalCount.textContent = data.total ?? 0;
        el.successCount.textContent = data.successful ?? 0;
        el.failedCount.textContent = data.failed ?? 0;
        el.resultMessage.textContent = `Generation completed: ${data.successful ?? 0} successful and ${data.failed ?? 0} failed.`;

        el.skippedNote.classList.toggle("hidden", !(skipped && skipped.length));
        if (skipped?.length) {
            el.skippedNote.replaceChildren(h("div", { class: "v-icon", text: "i" }), h("div", {},
                h("h3", { text: `${plural(skipped.length, "participant")} skipped` }),
                h("p", { text: "No certificate was created for: " + skipped.map((s) => `${s.participant} (row ${s.row})`).join(", ") + "." }),
            ));
        }

        el.jobStatusArea.classList.remove("hidden");
        el.jobIdText.textContent = `#${String(data.job_id).slice(0, 8)}`;
        el.jobStatusLink.href = data.status_url || "#";

        const items = (data.results || []).map((item) => {
            const ok = item.status === "success";
            const who = item.participant ? `${item.participant}${item.row ? ` · row ${item.row}` : ""}` : item.certificate_id;
            return h("div", { class: `result-item ${ok ? "" : "failed"}` },
                h("div", { class: "result-person" }, h("strong", { text: who }),
                    h("span", { text: ok ? `Certificate ready · ${item.certificate_id}` : (item.error || "Generation failed") })),
                ok && item.download_url ? h("a", { class: "result-link", href: item.download_url, text: "Download PDF" }) : null);
        });
        el.individualResults.replaceChildren(...items);
        if (data.download_url) {
            el.individualResults.append(h("a", {
                class: "result-link zip-download-link", href: data.download_url,
                style: "display:inline-flex", text: "Download all certificates (ZIP)",
            }));
        }
        setStep(4);
        el.resultPanel.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    async function generateCertificates() {
        const positions = getPositions();
        const skip = !el.skipInvalidWrap.classList.contains("hidden") && el.skipInvalid.checked;

        let response;
        try {
            response = await fetch("/api/certificates/bulk", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    upload_id: previewData.upload_id,
                    ...currentChecks(),
                    positions,
                    on_invalid: skip ? "skip" : "reject",
                }),
            });
        } catch (error) {
            throw new Error("Could not reach the server. Check your connection and try again.");
        }

        let job;
        try {
            job = await readJson(response);
        } catch (error) {
            if (error.data?.report) {  // the server found a data problem: show it in full
                report = error.data.report;
                renderAll();
                el.validationPanel.scrollIntoView({ behavior: "smooth", block: "start" });
                throw new Error("Generation was blocked because of the data problems shown in the data check.");
            }
            throw error;
        }

        let finished = job;
        if (response.status === 202) {
            showProgress(0, job.total, `Queued… ${job.total} certificates`);
            finished = await pollJob(job);
        }
        showProgress(finished.total, finished.total, "Done");
        showResults(finished, job.skipped);
        setStatus(
            `Finished. ${finished.successful ?? 0} certificates generated; ${finished.failed ?? 0} failed.` +
            (job.skipped?.length ? ` ${plural(job.skipped.length, "participant")} skipped.` : ""),
            finished.failed ? "warning" : "success");
    }

    // ------------------------------------------------------------------
    // Events
    // ------------------------------------------------------------------
    el.templateFile.addEventListener("change", () => {
        clearPreviewState();
        showSelectedFile(el.templateFile, el.templateInfo, "Template");
        if (el.templateFile.files[0]) showTemplatePreview(el.templateFile.files[0]);
        el.previewButton.disabled = !(el.templateFile.files.length && el.participantFile.files.length);
        setStatus("Template selected. Now select the participant file.");
    });

    el.participantFile.addEventListener("change", () => {
        clearPreviewState();
        showSelectedFile(el.participantFile, el.participantInfo, "Participant file");
        const ready = el.templateFile.files.length && el.participantFile.files.length;
        el.previewButton.disabled = !ready;
        if (recheckAfterReupload && ready) {
            recheckAfterReupload = false;
            el.previewButton.click();  // re-check the corrected file straight away
            return;
        }
        recheckAfterReupload = false;
        setStatus("Participant file selected. Click “Check my files” to review the data.");
    });

    el.previewButton.addEventListener("click", async () => {
        if (busy) return;
        busy = true;
        el.previewButton.disabled = true;
        setStatus("Uploading files and checking your data…");
        try {
            await uploadAndPreview();
        } catch (error) {
            setStatus(error.message, "error");
            el.previewButton.disabled = false;
        } finally {
            busy = false;
            updateGenerateAvailability();
        }
    });

    el.skipInvalid.addEventListener("change", updateGenerateAvailability);

    el.samplePreviewButton.addEventListener("click", async () => {
        el.samplePreviewButton.disabled = true;
        setStatus("Creating sample certificate preview…");
        try {
            await previewFirstParticipant();
        } catch (error) {
            setStatus(error.message, "error");
        } finally {
            el.samplePreviewButton.disabled = false;
        }
    });

    el.generateButton.addEventListener("click", async () => {
        if (busy) return;
        busy = true;
        el.generateButton.disabled = true;
        setStep(3);
        setStatus("Generating certificates. You can keep this page open; large batches take a little while…");
        try {
            await generateCertificates();
        } catch (error) {
            setStatus(error.message, "error");
            el.progressWrap.classList.add("hidden");
            setStep(2);
        } finally {
            busy = false;
            updateGenerateAvailability();
        }
    });

    el.fontUploadButton.addEventListener("click", () => {
        if (!previewData) { setStatus("Check your files first, then add a font.", "warning"); return; }
        el.fontUploadInput.click();
    });

    el.fontUploadInput.addEventListener("change", async () => {
        const file = el.fontUploadInput.files[0];
        el.fontUploadInput.value = "";
        if (!file) return;
        try {
            await uploadFont(file);
        } catch (error) {
            setStatus(error.message, "error");
        }
    });

    /** Put a file into a file input and let the normal "file chosen" logic run. */
    function setInputFile(input, file) {
        const transfer = new DataTransfer();
        transfer.items.add(file);
        input.files = transfer.files;
        input.dispatchEvent(new Event("change", { bubbles: true }));
    }

    async function loadSampleFiles() {
        const fetchFile = async (name, type) => {
            const response = await fetch(`/examples/${name}`);
            if (!response.ok) throw new Error("The sample files could not be loaded.");
            return new File([await response.blob()], name, { type });
        };
        setInputFile(el.templateFile, await fetchFile("sample_certificate_template.pdf", "application/pdf"));
        setInputFile(el.participantFile, await fetchFile("sample_participants.csv", "text/csv"));
        setStatus("Sample files loaded. Click “Check my files” to continue.", "success");
    }

    el.sampleButton.addEventListener("click", async () => {
        if (busy) return;
        el.sampleButton.disabled = true;
        try {
            await loadSampleFiles();
        } catch (error) {
            setStatus(error.message, "error");
        } finally {
            el.sampleButton.disabled = false;
        }
    });

    function setupDragAndDrop(area, input) {
        if (!area || !input) return;
        ["dragenter", "dragover"].forEach((name) => area.addEventListener(name, (event) => {
            event.preventDefault();
            area.classList.add("dragover");
        }));
        ["dragleave", "drop"].forEach((name) => area.addEventListener(name, (event) => {
            event.preventDefault();
            area.classList.remove("dragover");
        }));
        area.addEventListener("drop", (event) => {
            const files = event.dataTransfer?.files;
            if (!files || !files.length) return;
            const transfer = new DataTransfer();
            [...files].forEach((file) => transfer.items.add(file));
            input.files = transfer.files;
            input.dispatchEvent(new Event("change", { bubbles: true }));
        });
    }
    setupDragAndDrop($("templateDropArea"), el.templateFile);
    setupDragAndDrop($("participantDropArea"), el.participantFile);

    window.addEventListener("beforeunload", () => {
        if (templateObjectUrl) URL.revokeObjectURL(templateObjectUrl);
        revokeSamplePreview();
    });

    // Initial state
    el.previewButton.disabled = true;
    el.generateButton.disabled = true;
    setStep(1);
})();