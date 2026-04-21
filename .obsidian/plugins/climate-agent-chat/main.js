const {
  ItemView,
  Notice,
  Plugin,
  PluginSettingTab,
  Setting,
  requestUrl,
} = require("obsidian");

const VIEW_TYPE = "climate-agent-chat-view";
const DEFAULT_SETTINGS = {
  serverUrl: "http://localhost:8501",
};

class ClimateAgentView extends ItemView {
  constructor(leaf, plugin) {
    super(leaf);
    this.plugin = plugin;
    this.messages = [];
    this.answerMode = "detailed";
  }

  getViewType() {
    return VIEW_TYPE;
  }

  getDisplayText() {
    return "Climate Agent";
  }

  getIcon() {
    return "message-circle";
  }

  async onOpen() {
    this.draw();
    await this.checkConfig();
  }

  draw() {
    const root = this.containerEl.children[1];
    root.empty();
    root.addClass("climate-agent-view");

    const header = root.createDiv({ cls: "climate-agent-header" });
    header.createEl("h2", { text: "Climate Wiki Agent" });
    this.statusEl = header.createEl("span", {
      cls: "climate-agent-status",
      text: "Connecting",
    });

    this.messagesEl = root.createDiv({ cls: "climate-agent-messages" });
    this.renderEmptyState();

    this.sourcesEl = root.createDiv({ cls: "climate-agent-sources" });

    const form = root.createEl("form", { cls: "climate-agent-form" });
    this.inputEl = form.createEl("textarea", {
      attr: {
        rows: "3",
        placeholder: "Ask about climate risk, nat-cat insurance, IFRS S2, IAIS...",
      },
    });
    const controls = form.createDiv({ cls: "climate-agent-controls" });
    const contextButton = controls.createEl("button", {
      text: "Use active note",
      type: "button",
    });
    const modeWrap = controls.createDiv({ cls: "climate-agent-mode-wrap" });
    modeWrap.createSpan({ cls: "climate-agent-mode-label", text: "Answer" });
    this.modeSelect = modeWrap.createEl("select", { cls: "climate-agent-mode" });
    this.modeSelect.createEl("option", { text: "Brief", value: "brief" });
    this.modeSelect.createEl("option", { text: "Detailed", value: "detailed" });
    this.modeSelect.value = this.answerMode;
    this.modeSelect.addEventListener("change", () => {
      this.answerMode = this.modeSelect.value || "detailed";
    });
    this.sendButton = controls.createEl("button", {
      text: "Send",
      type: "submit",
    });

    contextButton.addEventListener("click", () => {
      const file = this.app.workspace.getActiveFile();
      if (!file) {
        new Notice("No active note.");
        return;
      }
      this.inputEl.value = `Based on ${file.path}, summarize the key climate-risk implications.`;
      this.inputEl.focus();
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const text = this.inputEl.value.trim();
      if (!text) return;
      this.inputEl.value = "";
      await this.send(text);
    });
  }

  renderEmptyState() {
    this.messagesEl.empty();
    const empty = this.messagesEl.createDiv({ cls: "climate-agent-empty" });
    empty.createEl("p", {
      text: "Ask a question. The current active note is sent as extra context when available.",
    });
  }

  renderMessages() {
    this.messagesEl.empty();
    for (const message of this.messages) {
      const row = this.messagesEl.createDiv({
        cls: `climate-agent-message climate-agent-message-${message.role}`,
      });
      row.createDiv({ cls: "climate-agent-bubble", text: message.content });
    }
    this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
  }

  renderSources(sources) {
    this.sourcesEl.empty();
    if (!sources || sources.length === 0) {
      return;
    }
    this.sourcesEl.createEl("h3", { text: "Sources" });
    for (const source of sources) {
      const button = this.sourcesEl.createEl("button", {
        cls: "climate-agent-source",
        type: "button",
      });
      button.createSpan({
        cls: "climate-agent-source-title",
        text: `[${source.index}] ${source.title}`,
      });
      button.createSpan({
        cls: "climate-agent-source-path",
        text: source.heading || source.path,
      });
      button.addEventListener("click", async () => {
        const path = source.path || "";
        const file = this.app.vault.getAbstractFileByPath(path);
        if (!file) {
          new Notice(`Source not found in vault: ${path}`);
          return;
        }
        await this.app.workspace.openLinkText(path, "", false);
      });
    }
  }

  getActiveContextPath() {
    const file = this.app.workspace.getActiveFile();
    return file ? file.path : null;
  }

  async checkConfig() {
    try {
      const payload = await this.requestJson("/api/config", "GET");
      this.answerMode = payload.default_answer_mode || this.answerMode;
      if (this.modeSelect) {
        this.modeSelect.value = this.answerMode;
      }
      this.statusEl.setText(
        payload.agent_mode === "openai" ? `OpenAI: ${payload.model}` : "Offline demo",
      );
      this.statusEl.toggleClass("is-offline", payload.agent_mode !== "openai");
    } catch (error) {
      this.statusEl.setText("API offline");
      this.statusEl.addClass("is-offline");
    }
  }

  async send(text) {
    this.messages.push({ role: "user", content: text });
    this.messages.push({ role: "assistant", content: "Searching wiki..." });
    this.renderMessages();
    this.sendButton.disabled = true;

    try {
      const payload = await this.requestJson("/api/chat", "POST", {
        messages: this.messages
          .filter((message) => message.content !== "Searching wiki...")
          .map((message) => ({
            role: message.role,
            content: message.content,
          })),
        contextPath: this.getActiveContextPath(),
        language: "en",
        answerMode: this.answerMode,
      });
      this.messages[this.messages.length - 1] = {
        role: "assistant",
        content: payload.text,
      };
      this.answerMode = payload.answer_mode || this.answerMode;
      if (this.modeSelect) {
        this.modeSelect.value = this.answerMode;
      }
      this.renderMessages();
      this.renderSources(payload.sources || []);
      this.statusEl.setText(
        payload.agent_mode === "openai" ? `OpenAI: ${payload.model}` : "Offline demo",
      );
      this.statusEl.toggleClass("is-offline", payload.agent_mode !== "openai");
    } catch (error) {
      this.messages[this.messages.length - 1] = {
        role: "assistant",
        content: `Request failed: ${error.message}`,
      };
      this.renderMessages();
    } finally {
      this.sendButton.disabled = false;
      this.inputEl.focus();
    }
  }

  async requestJson(path, method, body) {
    const base = this.plugin.settings.serverUrl.replace(/\/$/, "");
    const response = await requestUrl({
      url: `${base}${path}`,
      method,
      headers: {
        "Content-Type": "application/json",
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (response.status < 200 || response.status >= 300) {
      throw new Error(response.text || `HTTP ${response.status}`);
    }
    return response.json || JSON.parse(response.text);
  }
}

class ClimateAgentSettingTab extends PluginSettingTab {
  constructor(app, plugin) {
    super(app, plugin);
    this.plugin = plugin;
  }

  display() {
    const { containerEl } = this;
    containerEl.empty();
    containerEl.createEl("h2", { text: "Climate Agent Chat" });

    new Setting(containerEl)
      .setName("Agent API server URL")
      .setDesc("Run uvicorn api_server:app --host 0.0.0.0 --port 8501, then keep this URL on localhost.")
      .addText((text) =>
        text
          .setPlaceholder("http://localhost:8501")
          .setValue(this.plugin.settings.serverUrl)
          .onChange(async (value) => {
            this.plugin.settings.serverUrl = value.trim() || DEFAULT_SETTINGS.serverUrl;
            await this.plugin.saveSettings();
          }),
      );
  }
}

module.exports = class ClimateAgentPlugin extends Plugin {
  async onload() {
    await this.loadSettings();
    this.registerView(VIEW_TYPE, (leaf) => new ClimateAgentView(leaf, this));
    this.addRibbonIcon("message-circle", "Open Climate Agent", () => {
      this.activateView();
    });
    this.addCommand({
      id: "open-climate-agent-chat",
      name: "Open Climate Agent Chat",
      callback: () => this.activateView(),
    });
    this.addSettingTab(new ClimateAgentSettingTab(this.app, this));
  }

  async onunload() {
    this.app.workspace.detachLeavesOfType(VIEW_TYPE);
  }

  async loadSettings() {
    this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());
  }

  async saveSettings() {
    await this.saveData(this.settings);
  }

  async activateView() {
    let leaf = this.app.workspace.getLeavesOfType(VIEW_TYPE)[0];
    if (!leaf) {
      leaf = this.app.workspace.getRightLeaf(false);
      await leaf.setViewState({ type: VIEW_TYPE, active: true });
    }
    this.app.workspace.revealLeaf(leaf);
  }
};
