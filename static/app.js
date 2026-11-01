(() => {
  const byId = (id) => document.getElementById(id);
  const state = {
    username: null,
    conversation: null,
    conversationRevision: 0,
    conversationLoading: false,
    pendingConversationMessages: [],
    knownMessages: new Set(),
    hasConnected: false,
    socket: null,
    shouldReconnect: false,
    reconnectTimer: null,
    reconnectDelay: 900,
    heartbeatTimer: null,
    pending: new Map(),
    seenMessages: new Set(),
    hasOlderMessages: false,
    loadingOlderMessages: false,
    attachmentFile: null,
    attachmentPreviewUrl: null,
    uploadedAttachmentId: null,
    attachmentSendId: null,
    pendingTextValue: null,
    pendingTextId: null,
    isSending: false,
  };

  const joinView = byId("joinView");
  const chatView = byId("chatView");
  const joinForm = byId("joinForm");
  const usernameInput = byId("usernameInput");
  const usernameField = byId("usernameField");
  const usernameCount = byId("usernameCount");
  const inputCheck = byId("inputCheck");
  const joinError = byId("joinError");
  const joinButton = byId("joinButton");
  const leaveButton = byId("leaveButton");
  const messageInput = byId("messageInput");
  const charCount = byId("charCount");
  const sendButton = byId("sendButton");
  const attachButton = byId("attachButton");
  const removeAttachmentButton = byId("removeAttachment");
  const fileInput = byId("fileInput");
  const attachmentPreview = byId("attachmentPreview");
  const attachmentImage = byId("attachmentImage");
  const attachmentFileType = byId("attachmentFileType");
  const attachmentName = byId("attachmentName");
  const attachmentSize = byId("attachmentSize");
  const messageList = byId("messageList");
  const emptyChat = byId("emptyChat");
  const connectionPill = byId("connectionPill");
  const connectionText = byId("connectionText");
  const memberList = byId("memberList");
  const memberEmpty = byId("memberEmpty");
  const memberCount = byId("memberCount");
  const topMemberCount = byId("topMemberCount");
  const toastRegion = byId("toastRegion");
  const sharedRoomButton = byId("sharedRoomButton");
  const activeConversationLabel = byId("activeConversationLabel");
  const returnToRoomButton = byId("returnToRoom");
  const conversationEyebrow = byId("conversationEyebrow");
  const introTitleFirst = byId("introTitleFirst");
  const introTitleSecond = byId("introTitleSecond");
  const emptyChatTitle = byId("emptyChatTitle");
  const emptyChatDescription = byId("emptyChatDescription");
  const imageDialog = byId("imageDialog");
  const dialogImage = byId("dialogImage");
  const appFrame = byId("appFrame");
  const peopleButton = byId("peopleButton");
  const sidePanel = byId("sidePanel");
  const panelCloseButton = byId("panelClose");
  const mainPanel = byId("mainPanel");
  const sideScrim = byId("sideScrim");
  const mobilePanelQuery = window.matchMedia("(max-width: 760px)");

  function codePointLength(value) {
    return Array.from(value).length;
  }

  function normalizedUsername(value) {
    return value.normalize("NFKC").trim();
  }

  function usernameIsValid(value) {
    const name = normalizedUsername(value);
    const points = Array.from(name);
    if (points.length < 1 || points.length > 24) return false;
    return points.every((character, index) => {
      if (/[\p{L}\p{N}_]/u.test(character)) return true;
      return index > 0 && /\p{M}/u.test(character);
    });
  }

  function showConnection(text, mode) {
    connectionText.textContent = text;
    connectionPill.className = `connectionPill ${mode || ""}`.trim();
  }

  function showToast(message, isError = false) {
    toastRegion.replaceChildren();
    const toast = document.createElement("div");
    toast.className = `toast${isError ? " isError" : ""}`;
    const text = document.createElement("div");
    text.className = "toastText";
    text.textContent = message;
    const close = createNoticeClose();
    toast.append(text, close);
    toastRegion.append(toast);
  }

  function createNoticeClose() {
    const close = document.createElement("button");
    close.type = "button";
    close.className = "toastClose";
    close.setAttribute("aria-label", "Close notification");
    close.textContent = "×";
    close.addEventListener("click", () => toastRegion.replaceChildren());
    return close;
  }

  function showMessageNotice(message) {
    if (message.senderName === state.username) return;
    toastRegion.replaceChildren();
    const toast = document.createElement("div");
    toast.className = "toast pushNotice isMessageNotice";
    const avatar = document.createElement("span");
    avatar.className = `memberAvatar toastAvatar tone${toneFor(message.senderName || "?")}`;
    avatar.textContent = initialsFor(message.senderName || "?");
    avatar.setAttribute("aria-hidden", "true");
    const action = document.createElement("button");
    action.type = "button";
    action.className = "toastAction";
    const context = document.createElement("span");
    context.className = "toastKicker";
    context.textContent = message.recipientName
      ? "PRIVATE NOTE"
      : "SHARED ROOM";
    const title = document.createElement("strong");
    title.textContent = `A note from ${message.senderName}`;
    const preview = document.createElement("span");
    const content =
      message.kind === "image"
        ? "Shared a photo"
        : message.kind === "file"
          ? `Shared a file: ${message.fileName || "file"}`
          : (message.text || "").replace(/\s+/gu, " ").trim();
    preview.textContent =
      Array.from(content).slice(0, 120).join("") || "Open the conversation";
    action.append(context, title, preview);
    action.setAttribute(
      "aria-label",
      message.recipientName
        ? `Open private chat with ${message.senderName}`
        : "Open the shared room",
    );
    action.addEventListener("click", () => {
      if (message.recipientName) openDirectChat(message.senderName);
      else openSharedRoom();
      if (!state.isSending) toastRegion.replaceChildren();
    });
    toast.append(avatar, action, createNoticeClose());
    toastRegion.append(toast);
  }

  function showJoin(message = "") {
    toastRegion.replaceChildren();
    state.shouldReconnect = false;
    clearTimeout(state.reconnectTimer);
    clearInterval(state.heartbeatTimer);
    if (state.socket && state.socket.readyState < WebSocket.CLOSING) {
      state.socket.close(1000, "Leaving room");
    }
    state.socket = null;
    rejectPending("Your connection ended. Try again.");
    state.username = null;
    state.conversation = null;
    state.conversationLoading = false;
    state.conversationRevision += 1;
    state.pendingConversationMessages = [];
    state.knownMessages.clear();
    state.hasConnected = false;
    state.pendingTextValue = null;
    state.pendingTextId = null;
    clearAttachment();
    messageInput.value = "";
    updateMessageComposer();
    renderMembers([]);
    updateConversationDisplay();
    chatView.hidden = true;
    joinView.hidden = false;
    leaveButton.hidden = true;
    showConnection("Ready when you are", "");
    if (message) setJoinError(message);
    window.setTimeout(() => usernameInput.focus(), 40);
  }

  function setJoinError(message) {
    joinError.textContent = message;
    joinError.hidden = !message;
    usernameField.classList.toggle("hasError", Boolean(message));
  }

  function updateUsernameField() {
    const normalized = normalizedUsername(usernameInput.value);
    const points = Array.from(normalized);
    if (points.length > 24) {
      usernameInput.value = points.slice(0, 24).join("");
    }
    const count = codePointLength(normalizedUsername(usernameInput.value));
    usernameCount.textContent = `${count} of 24`;
    const valid = usernameIsValid(usernameInput.value);
    usernameField.classList.toggle("isValid", valid);
    if (!usernameField.classList.contains("hasError")) {
      inputCheck.textContent = valid ? "✓" : "";
    }
    if (joinError.textContent) setJoinError("");
  }

  function setActiveChat(username) {
    state.username = username;
    state.conversation = null;
    updateConversationDisplay();
    joinView.hidden = true;
    chatView.hidden = false;
    chatView.classList.remove("hasMessages");
    leaveButton.hidden = false;
    showConnection("Connecting", "isWaiting");
    byId("introDescription").textContent =
      `You are here as ${username}. Say hello when you are ready.`;
    connectSocket();
  }

  function usernameKey(value) {
    return normalizedUsername(value).toLocaleLowerCase();
  }

  function updateConversationDisplay() {
    const peer = state.conversation;
    const direct = Boolean(peer);
    activeConversationLabel.textContent = direct ? peer : "Shared room";
    activeConversationLabel.title = direct
      ? `Private chat with ${peer}`
      : "Shared room";
    topMemberCount.textContent = direct ? "Private conversation" : "Just you";
    returnToRoomButton.hidden = !direct;
    sharedRoomButton.classList.toggle("isActive", !direct);
    chatView.setAttribute(
      "aria-label",
      direct ? `Private chat with ${peer}` : "Shared room conversation",
    );
    conversationEyebrow.textContent = direct
      ? "A PRIVATE CONVERSATION"
      : "THE SHARED ROOM";
    introTitleFirst.textContent = direct ? "A quieter space" : "Good things";
    introTitleSecond.textContent = direct ? "for two." : "start here.";
    byId("introDescription").textContent = direct
      ? `Only you and ${peer} can read these messages.`
      : `You are here as ${state.username || ""}. Say hello when you are ready.`;
    emptyChatTitle.textContent = direct
      ? "Your chat is ready"
      : "The room is yours";
    emptyChatDescription.textContent = direct
      ? `Send a note or share a file with ${peer}.`
      : "Start with a hello, or share a file.";
    messageInput.placeholder = direct
      ? "Write a private message..."
      : "Write a message...";
    messageInput.setAttribute(
      "aria-label",
      direct ? `Write a private message to ${peer}` : "Write a message",
    );
    sendButton.setAttribute(
      "aria-label",
      direct ? "Send private message" : "Send message",
    );
    for (const memberButton of memberList.querySelectorAll("[data-name-key]")) {
      memberButton.classList.toggle(
        "isActive",
        direct && memberButton.dataset.nameKey === usernameKey(peer),
      );
    }
  }

  async function requestJson(url, options = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      ...options,
    });
    let body = {};
    try {
      body = await response.json();
    } catch {
      body = {};
    }
    if (!response.ok) {
      throw new Error(body.error || "That did not work. Try again.");
    }
    return body;
  }

  async function joinRoom(event) {
    event.preventDefault();
    setJoinError("");
    const username = normalizedUsername(usernameInput.value);
    if (!usernameIsValid(username)) {
      setJoinError(
        "Use 1 to 24 letters, numbers, or underscore for your username.",
      );
      usernameInput.focus();
      return;
    }
    joinButton.disabled = true;
    joinButton.querySelector("span").textContent = "Joining";
    try {
      const result = await requestJson("/api/join", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username }),
      });
      setActiveChat(result.name);
    } catch (error) {
      setJoinError(error.message);
      usernameField.classList.add("hasError");
      usernameInput.focus();
    } finally {
      joinButton.disabled = false;
      joinButton.querySelector("span").textContent = "Join the room";
    }
  }

  function createClientId() {
    const bytes = new Uint8Array(12);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join(
      "",
    );
  }

  function rejectPending(message) {
    for (const [clientId, pending] of state.pending) {
      clearTimeout(pending.timer);
      pending.reject(new Error(message));
      state.pending.delete(clientId);
    }
  }

  function sendSocketEvent(event, clientId = createClientId()) {
    if (!state.socket || state.socket.readyState !== WebSocket.OPEN) {
      return Promise.reject(
        new Error("Waiting for the connection. Try again."),
      );
    }
    return new Promise((resolve, reject) => {
      const timer = window.setTimeout(() => {
        state.pending.delete(clientId);
        reject(new Error("That message took too long. Try again."));
      }, 12000);
      state.pending.set(clientId, { resolve, reject, timer });
      state.socket.send(JSON.stringify({ ...event, clientId }));
    });
  }

  function settlePending(clientId, error, value) {
    if (!clientId || !state.pending.has(clientId)) return;
    const pending = state.pending.get(clientId);
    state.pending.delete(clientId);
    clearTimeout(pending.timer);
    if (error) pending.reject(new Error(error));
    else pending.resolve(value);
  }

  function connectSocket() {
    if (!state.username || (!state.shouldReconnect && state.socket)) return;
    clearTimeout(state.reconnectTimer);
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${protocol}//${window.location.host}/ws`);
    let reconnecting = false;
    state.socket = socket;
    state.shouldReconnect = true;

    socket.addEventListener("open", () => {
      if (state.socket !== socket) return;
      reconnecting = state.hasConnected;
      state.hasConnected = true;
      state.reconnectDelay = 900;
      showConnection("Connected", "isConnected");
      clearInterval(state.heartbeatTimer);
      state.heartbeatTimer = window.setInterval(() => {
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: "presence.heartbeat" }));
        }
      }, 20000);
    });

    socket.addEventListener("message", (event) => {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch {
        return;
      }
      handleServerEvent(payload, reconnecting);
    });

    socket.addEventListener("error", () => {
      showConnection("Connection paused", "isOffline");
    });

    socket.addEventListener("close", (event) => {
      clearInterval(state.heartbeatTimer);
      rejectPending("The connection paused. Try again.");
      if (state.socket !== socket || !state.shouldReconnect) return;
      if (event.code === 4001) {
        showJoin("Your session ended. Join again.");
        return;
      }
      showConnection("Reconnecting", "isWaiting");
      state.reconnectTimer = window.setTimeout(
        connectSocket,
        state.reconnectDelay,
      );
      state.reconnectDelay = Math.min(state.reconnectDelay * 1.7, 14000);
    });
  }

  function handleServerEvent(event, reconnecting = false) {
    if (!event || typeof event !== "object") return;
    if (event.type === "history") {
      const messages = Array.isArray(event.messages) ? event.messages : [];
      if (reconnecting) {
        for (const message of messages) {
          if (
            !state.knownMessages.has(message.id) &&
            message.senderName !== state.username
          ) {
            showMessageNotice(message);
          }
          state.knownMessages.add(message.id);
        }
        if (state.conversation) {
          openDirectChat(state.conversation, true);
          return;
        }
      } else {
        for (const message of messages) state.knownMessages.add(message.id);
      }
      if (!state.conversation) {
        renderHistory(messages, messages.length >= 100);
      }
      return;
    }
    if (event.type === "presence.snapshot") {
      renderMembers(Array.isArray(event.members) ? event.members : []);
      return;
    }
    if (event.type === "message.created" && event.message) {
      settlePending(event.clientId, null, event.message);
      const isNew = !state.knownMessages.has(event.message.id);
      state.knownMessages.add(event.message.id);
      if (isNew) showMessageNotice(event.message);
      if (messageMatchesCurrentConversation(event.message)) {
        if (state.conversationLoading)
          state.pendingConversationMessages.push(event.message);
        else renderMessage(event.message);
      }
      return;
    }
    if (event.type === "error") {
      settlePending(event.clientId, event.message || "That did not work.");
      if (!event.clientId)
        showToast(event.message || "That did not work.", true);
      return;
    }
    if (event.type === "session.ended") {
      showJoin(event.message || "Your session ended. Join again.");
    }
  }

  function toneFor(name) {
    let value = 0;
    for (const character of name)
      value = (value * 31 + character.codePointAt(0)) >>> 0;
    return value % 6;
  }

  function initialsFor(name) {
    return Array.from(name.trim())[0]?.toLocaleUpperCase() || "?";
  }

  function renderMembers(members) {
    memberList.replaceChildren();
    memberCount.textContent = String(members.length);
    topMemberCount.textContent = state.conversation
      ? "Private conversation"
      : members.length === 1
        ? "1 person here"
        : `${members.length} people here`;
    if (!members.length) {
      memberList.append(memberEmpty);
      return;
    }
    for (const member of members) {
      const isYou = member.name === state.username;
      const row = document.createElement(isYou ? "div" : "button");
      row.className = `memberRow${isYou ? " memberButton isYou" : " memberButton"}`;
      if (!isYou) {
        row.type = "button";
        row.classList.toggle(
          "isActive",
          Boolean(
            state.conversation &&
            usernameKey(member.name) === usernameKey(state.conversation),
          ),
        );
        row.dataset.nameKey = usernameKey(member.name);
        row.setAttribute(
          "aria-label",
          `Start a private chat with ${member.name}`,
        );
        row.addEventListener("click", () => openDirectChat(member.name));
      }
      const avatar = document.createElement("span");
      avatar.className = `memberAvatar tone${toneFor(member.name)}`;
      avatar.textContent = initialsFor(member.name);
      avatar.setAttribute("aria-hidden", "true");
      const details = document.createElement("span");
      details.className = "memberDetails";
      const name = document.createElement("span");
      name.className = "memberName";
      name.textContent = member.name;
      details.append(name);
      if (isYou) {
        const hint = document.createElement("span");
        hint.className = "memberHint";
        hint.textContent = "You";
        details.append(hint);
      } else {
        const hint = document.createElement("span");
        hint.className = "memberHint";
        hint.textContent = "Private chat";
        details.append(hint);
      }
      const dot = document.createElement("span");
      dot.className = "memberDot";
      dot.setAttribute("aria-hidden", "true");
      row.append(avatar, details, dot);
      memberList.append(row);
    }
  }

  function formatTime(timestamp) {
    const date = new Date(timestamp * 1000);
    if (Number.isNaN(date.getTime())) return "Now";
    return new Intl.DateTimeFormat(undefined, {
      hour: "numeric",
      minute: "2-digit",
    }).format(date);
  }

  function renderHistory(messages, hasMore = messages.length >= 100) {
    state.seenMessages.clear();
    state.loadingOlderMessages = false;
    state.hasOlderMessages = Boolean(hasMore);
    messageList.replaceChildren();
    if (!messages.length) {
      messageList.append(emptyChat);
      chatView.classList.remove("hasMessages");
      return;
    }
    chatView.classList.add("hasMessages");
    for (const message of messages) renderMessage(message, false);
    messageList.scrollTop = messageList.scrollHeight;
  }

  function messageMatchesCurrentConversation(message) {
    if (!message.recipientName) return !state.conversation;
    if (!state.conversation) return false;
    const peer =
      message.senderName === state.username
        ? message.recipientName
        : message.senderName;
    return usernameKey(peer) === usernameKey(state.conversation);
  }

  function mergeConversationMessages(history, liveMessages) {
    const messages = new Map(history.map((message) => [message.id, message]));
    for (const message of liveMessages) messages.set(message.id, message);
    return [...messages.values()].sort(
      (first, second) =>
        first.createdAt - second.createdAt || first.id.localeCompare(second.id),
    );
  }

  function resetConversationView() {
    state.seenMessages.clear();
    state.hasOlderMessages = false;
    state.loadingOlderMessages = false;
    state.pendingTextValue = null;
    state.pendingTextId = null;
    state.attachmentSendId = null;
    messageList.replaceChildren(emptyChat);
    chatView.classList.remove("hasMessages");
    updateConversationDisplay();
  }

  async function openDirectChat(name, notifyNew = false) {
    if (state.isSending) {
      showToast(
        "Your message is still sending. Please wait before switching chats.",
      );
      return;
    }
    if (!state.username || usernameKey(name) === usernameKey(state.username))
      return;
    state.conversation = normalizedUsername(name);
    const revision = ++state.conversationRevision;
    state.conversationLoading = true;
    state.pendingConversationMessages = [];
    resetConversationView();
    if (appFrame.classList.contains("panelOpen")) closePeoplePanel(false);
    try {
      const page = await requestJson(
        `/api/history?with=${encodeURIComponent(state.conversation)}`,
      );
      if (revision !== state.conversationRevision) return;
      const messages = mergeConversationMessages(
        Array.isArray(page.messages) ? page.messages : [],
        state.pendingConversationMessages,
      );
      for (const message of messages) {
        if (
          notifyNew &&
          !state.knownMessages.has(message.id) &&
          message.senderName !== state.username
        ) {
          showMessageNotice(message);
        }
        state.knownMessages.add(message.id);
      }
      state.conversationLoading = false;
      state.pendingConversationMessages = [];
      renderHistory(messages, page.hasMore);
      messageInput.focus();
    } catch (error) {
      if (revision !== state.conversationRevision) return;
      const messages = mergeConversationMessages(
        [],
        state.pendingConversationMessages,
      );
      for (const message of messages) state.knownMessages.add(message.id);
      state.conversationLoading = false;
      state.pendingConversationMessages = [];
      renderHistory(messages, false);
      showToast(error.message, true);
    }
  }

  async function openSharedRoom() {
    if (state.isSending) {
      showToast(
        "Your message is still sending. Please wait before switching chats.",
      );
      return;
    }
    if (!state.username || !state.conversation) return;
    state.conversation = null;
    const revision = ++state.conversationRevision;
    state.conversationLoading = true;
    state.pendingConversationMessages = [];
    resetConversationView();
    if (appFrame.classList.contains("panelOpen")) closePeoplePanel(false);
    try {
      const page = await requestJson("/api/history");
      if (revision !== state.conversationRevision) return;
      const messages = mergeConversationMessages(
        Array.isArray(page.messages) ? page.messages : [],
        state.pendingConversationMessages,
      );
      for (const message of messages) state.knownMessages.add(message.id);
      state.conversationLoading = false;
      state.pendingConversationMessages = [];
      renderHistory(messages, page.hasMore);
      messageInput.focus();
    } catch (error) {
      if (revision !== state.conversationRevision) return;
      const messages = mergeConversationMessages(
        [],
        state.pendingConversationMessages,
      );
      for (const message of messages) state.knownMessages.add(message.id);
      state.conversationLoading = false;
      state.pendingConversationMessages = [];
      renderHistory(messages, false);
      showToast(error.message, true);
    }
  }

  function renderMessage(message, scroll = true, prepend = false) {
    if (!message || !message.id || state.seenMessages.has(message.id))
      return null;
    state.seenMessages.add(message.id);
    if (emptyChat.isConnected) emptyChat.remove();
    chatView.classList.add("hasMessages");
    const wasNearBottom =
      messageList.scrollHeight -
        messageList.clientHeight -
        messageList.scrollTop <
      90;
    const own = message.senderName === state.username;
    const row = document.createElement("article");
    row.className = `messageRow${own ? " ownMessage" : ""}`;
    row.dataset.messageId = message.id;
    const avatar = document.createElement("span");
    avatar.className = `messageAvatar tone${toneFor(message.senderName || "?")}`;
    avatar.textContent = initialsFor(message.senderName || "?");
    avatar.setAttribute("aria-hidden", "true");

    const content = document.createElement("div");
    content.className = "messageContent";
    const meta = document.createElement("div");
    meta.className = "messageMeta";
    const sender = document.createElement("strong");
    sender.textContent = own ? "You" : message.senderName;
    const time = document.createElement("span");
    time.textContent = formatTime(message.createdAt);
    meta.append(sender, time);

    if (message.kind === "image" && typeof message.mediaUrl === "string") {
      const imageButton = document.createElement("button");
      imageButton.type = "button";
      imageButton.className = "messageImageButton";
      imageButton.setAttribute(
        "aria-label",
        `Open image shared by ${message.senderName}`,
      );
      const image = document.createElement("img");
      image.src = message.mediaUrl;
      image.alt = `Image shared by ${message.senderName}`;
      image.loading = "lazy";
      image.addEventListener(
        "error",
        () => {
          image.alt = "This image is no longer available.";
        },
        { once: true },
      );
      imageButton.append(image);
      imageButton.addEventListener("click", () =>
        openImage(message.mediaUrl, image.alt),
      );
      content.append(meta, imageButton);
    } else if (message.kind === "file" && typeof message.fileUrl === "string") {
      const card = document.createElement("a");
      card.className = "fileMessageCard";
      card.href = message.fileUrl;
      card.download = message.fileName || "shared file";
      card.setAttribute(
        "aria-label",
        `Download ${message.fileName || "shared file"}`,
      );
      const badge = document.createElement("span");
      badge.className = `fileTypeBadge ${fileBadgeClass(message.fileCategory)}`;
      badge.textContent = fileBadgeText(message.fileCategory);
      badge.setAttribute("aria-hidden", "true");
      const info = document.createElement("span");
      info.className = "fileMessageInfo";
      const filename = document.createElement("strong");
      filename.textContent = message.fileName || "Shared file";
      const details = document.createElement("span");
      details.textContent = `${fileCategoryLabel(message.fileCategory)} · ${formatSize(message.fileSize || 0)}`;
      info.append(filename, details);
      card.append(badge, info);
      content.append(meta, card);
    } else {
      const bubble = document.createElement("div");
      bubble.className = "messageBubble";
      bubble.textContent = message.text || "";
      content.append(meta, bubble);
    }
    row.append(avatar, content);
    if (prepend) messageList.prepend(row);
    else messageList.append(row);
    if (scroll && wasNearBottom)
      messageList.scrollTop = messageList.scrollHeight;
    return row;
  }

  async function loadOlderMessages() {
    if (state.loadingOlderMessages || !state.hasOlderMessages) return;
    const firstMessage = messageList.querySelector(".messageRow");
    if (!firstMessage?.dataset.messageId) return;
    state.loadingOlderMessages = true;
    const previousHeight = messageList.scrollHeight;
    const previousTop = messageList.scrollTop;
    try {
      const page = await requestJson(
        `/api/history?before=${encodeURIComponent(firstMessage.dataset.messageId)}` +
          (state.conversation
            ? `&with=${encodeURIComponent(state.conversation)}`
            : ""),
      );
      const messages = Array.isArray(page.messages) ? page.messages : [];
      for (const message of messages) state.knownMessages.add(message.id);
      state.hasOlderMessages = Boolean(page.hasMore);
      for (const message of [...messages].reverse()) {
        renderMessage(message, false, true);
      }
      messageList.scrollTop =
        previousTop + messageList.scrollHeight - previousHeight;
    } catch (error) {
      showToast(error.message, true);
      state.hasOlderMessages = false;
    } finally {
      state.loadingOlderMessages = false;
    }
  }

  function openImage(url, alt) {
    dialogImage.src = url;
    dialogImage.alt = alt || "Shared image";
    imageDialog.showModal();
  }

  function updateMessageComposer() {
    const count = codePointLength(messageInput.value);
    charCount.textContent = `${count} of 3000`;
    charCount.classList.toggle("nearLimit", count > 2800);
    sendButton.disabled =
      state.isSending || (!messageInput.value.trim() && !state.attachmentFile);
    messageInput.disabled = state.isSending;
    attachButton.disabled = state.isSending;
    removeAttachmentButton.disabled = state.isSending;
    fileInput.disabled = state.isSending;
    leaveButton.disabled = state.isSending;
  }

  function formatSize(bytes) {
    if (bytes < 1_000_000) return `${Math.max(1, Math.round(bytes / 1000))} KB`;
    return `${(bytes / 1_000_000).toFixed(1)} MB`;
  }

  function fileBadgeText(category) {
    return (
      {
        image: "IMG",
        video: "MP4",
        audio: "MP3",
        text: "TXT",
        pdf: "PDF",
        document: "DOCX",
      }[category] || "FILE"
    );
  }

  function fileBadgeClass(category) {
    return (
      {
        video: "isVideo",
        audio: "isAudio",
        text: "isText",
        pdf: "isPdf",
        document: "isDocument",
      }[category] || "isFile"
    );
  }

  function fileCategoryLabel(category) {
    return (
      {
        video: "Video",
        audio: "Audio",
        text: "Text file",
        pdf: "PDF document",
        document: "Word document",
      }[category] || "File"
    );
  }

  function setAttachment(file) {
    clearAttachment();
    state.attachmentFile = file;
    state.uploadedAttachmentId = null;
    state.attachmentSendId = null;
    const isImage =
      file.type.startsWith("image/") || /\.(jpe?g|png|webp)$/iu.test(file.name);
    attachmentImage.hidden = !isImage;
    attachmentFileType.hidden = isImage;
    if (isImage) {
      state.attachmentPreviewUrl = URL.createObjectURL(file);
      attachmentImage.src = state.attachmentPreviewUrl;
    } else {
      attachmentImage.removeAttribute("src");
      const extension = file.name.split(".").pop()?.toLocaleLowerCase();
      const category =
        {
          mp4: "video",
          mp3: "audio",
          txt: "text",
          pdf: "pdf",
          docx: "document",
        }[extension] || "file";
      attachmentFileType.className = `attachmentFileType ${fileBadgeClass(category)}`;
      attachmentFileType.textContent = fileBadgeText(category);
    }
    attachmentName.textContent = file.name || "Image";
    attachmentSize.textContent = formatSize(file.size);
    attachmentPreview.hidden = false;
    updateMessageComposer();
  }

  function clearAttachment() {
    if (state.attachmentPreviewUrl)
      URL.revokeObjectURL(state.attachmentPreviewUrl);
    state.attachmentPreviewUrl = null;
    state.attachmentFile = null;
    state.uploadedAttachmentId = null;
    state.attachmentSendId = null;
    attachmentImage.removeAttribute("src");
    attachmentImage.hidden = true;
    attachmentFileType.hidden = true;
    attachmentPreview.hidden = true;
    fileInput.value = "";
    updateMessageComposer();
  }

  async function uploadAttachment() {
    if (state.uploadedAttachmentId) return state.uploadedAttachmentId;
    if (!state.attachmentFile) return null;
    const form = new FormData();
    form.append(
      "file",
      state.attachmentFile,
      state.attachmentFile.name || "file",
    );
    const result = await requestJson("/api/upload", {
      method: "POST",
      body: form,
    });
    state.uploadedAttachmentId = result.mediaId;
    return result.mediaId;
  }

  async function sendCurrentMessage() {
    if (state.isSending) return;
    const text = messageInput.value;
    const hasText = Boolean(text.trim());
    if (!hasText && !state.attachmentFile) return;
    if (codePointLength(text) > 3000) {
      showToast("Messages can contain up to 3000 characters.", true);
      return;
    }
    const recipient = state.conversation;
    state.isSending = true;
    sendButton.querySelector("span").textContent = "Sending";
    updateMessageComposer();
    try {
      if (state.attachmentFile) {
        const mediaId = await uploadAttachment();
        state.attachmentSendId ||= createClientId();
        await sendSocketEvent(
          {
            type: "chat.sendAttachment",
            mediaId,
            ...(recipient ? { recipient } : {}),
          },
          state.attachmentSendId,
        );
        clearAttachment();
      }
      if (hasText) {
        if (state.pendingTextValue !== text) {
          state.pendingTextValue = text;
          state.pendingTextId = createClientId();
        }
        await sendSocketEvent(
          { type: "chat.sendText", text, ...(recipient ? { recipient } : {}) },
          state.pendingTextId,
        );
        state.pendingTextValue = null;
        state.pendingTextId = null;
        if (messageInput.value === text) messageInput.value = "";
        updateMessageComposer();
      }
    } catch (error) {
      showToast(error.message, true);
    } finally {
      state.isSending = false;
      sendButton.querySelector("span").textContent = "Send";
      updateMessageComposer();
      if (!state.attachmentFile && !messageInput.value) messageInput.focus();
    }
  }

  function openPeoplePanel() {
    appFrame.classList.add("panelOpen");
    syncPanelAccessibility();
    panelCloseButton.focus();
  }

  function closePeoplePanel(restoreFocus = true) {
    appFrame.classList.remove("panelOpen");
    syncPanelAccessibility();
    if (restoreFocus) peopleButton.focus();
    else messageInput.focus();
  }

  function focusMainContent() {
    (chatView.hidden ? usernameInput : messageInput).focus();
  }

  function syncPanelAccessibility() {
    const isMobile = mobilePanelQuery.matches;
    const wasOpen = appFrame.classList.contains("panelOpen");
    const focusWasInPanel = sidePanel.contains(document.activeElement);
    const focusWasOnToggle = document.activeElement === peopleButton;
    if (!isMobile) appFrame.classList.remove("panelOpen");
    const isOpen = isMobile && appFrame.classList.contains("panelOpen");
    sidePanel.inert = isMobile && !isOpen;
    sidePanel.setAttribute("aria-hidden", String(isMobile && !isOpen));
    mainPanel.inert = isOpen;
    mainPanel.setAttribute("aria-hidden", String(isOpen));
    sideScrim.inert = !isOpen;
    sideScrim.setAttribute("aria-hidden", String(!isOpen));
    peopleButton.setAttribute("aria-expanded", String(isOpen));
    if (isMobile && !isOpen && focusWasInPanel) peopleButton.focus();
    else if (!isMobile && (wasOpen || focusWasOnToggle)) focusMainContent();
  }

  async function leaveRoom() {
    if (state.isSending) {
      showToast(
        "Your message is still sending. Please wait before leaving the room.",
      );
      return;
    }
    leaveButton.disabled = true;
    try {
      await requestJson("/api/leave", { method: "POST" });
    } catch {
      showToast("Your room is closing. You can join again.");
    }
    state.shouldReconnect = false;
    if (state.socket && state.socket.readyState < WebSocket.CLOSING) {
      state.socket.close(1000, "Leaving room");
    }
    state.socket = null;
    clearAttachment();
    messageInput.value = "";
    updateMessageComposer();
    showJoin();
    leaveButton.disabled = false;
  }

  joinForm.addEventListener("submit", joinRoom);
  usernameInput.addEventListener("input", updateUsernameField);
  usernameInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      joinForm.requestSubmit();
    }
  });
  attachButton.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    if (file.size > 5_000_000) {
      showToast("Files must be 5 MB or smaller.", true);
      fileInput.value = "";
      return;
    }
    if (!/\.(jpe?g|png|webp|mp4|mp3|txt|pdf|docx)$/iu.test(file.name)) {
      showToast(
        "Choose an image, MP4 video, MP3 audio, TXT file, PDF, or DOCX document.",
        true,
      );
      fileInput.value = "";
      return;
    }
    setAttachment(file);
  });
  removeAttachmentButton.addEventListener("click", clearAttachment);
  messageInput.addEventListener("input", () => {
    const characters = Array.from(messageInput.value);
    if (characters.length > 3000)
      messageInput.value = characters.slice(0, 3000).join("");
    updateMessageComposer();
  });
  messageInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      sendCurrentMessage();
    }
  });
  messageList.addEventListener("scroll", () => {
    if (messageList.scrollTop < 38) loadOlderMessages();
  });
  sendButton.addEventListener("click", sendCurrentMessage);
  leaveButton.addEventListener("click", leaveRoom);
  sharedRoomButton.addEventListener("click", openSharedRoom);
  returnToRoomButton.addEventListener("click", openSharedRoom);
  panelCloseButton.addEventListener("click", () => closePeoplePanel());
  peopleButton.addEventListener("click", () => {
    if (appFrame.classList.contains("panelOpen")) closePeoplePanel();
    else openPeoplePanel();
  });
  sideScrim.addEventListener("click", closePeoplePanel);
  mobilePanelQuery.addEventListener("change", syncPanelAccessibility);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && appFrame.classList.contains("panelOpen")) {
      closePeoplePanel();
    }
  });
  byId("dialogClose").addEventListener("click", () => imageDialog.close());
  imageDialog.addEventListener("click", (event) => {
    if (event.target === imageDialog) imageDialog.close();
  });

  async function initialize() {
    syncPanelAccessibility();
    updateUsernameField();
    updateMessageComposer();
    try {
      const result = await requestJson("/api/me");
      setActiveChat(result.name);
    } catch {
      showJoin();
    }
  }

  initialize();
})();
