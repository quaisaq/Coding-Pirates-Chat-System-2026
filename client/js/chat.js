let socket;

const connectDiv = document.getElementById("connect");
const chatDiv = document.getElementById("chat");
const statusText = document.getElementById("status");
const messages = document.getElementById("messages");
const messageInput = document.getElementById("message");

function addMessage(text, cssClass) {
    const li = document.createElement("li");
    li.textContent = text;
    if (cssClass) li.className = cssClass;
    messages.appendChild(li);
    messages.scrollTop = messages.scrollHeight;
}

document.getElementById("connectBtn").addEventListener("click", () => {
    const ip = document.getElementById("ip").value.trim();
    const port = document.getElementById("port").value.trim();
    statusText.textContent = "Connecting...";

    socket = new WebSocket(`ws://${ip}:${port}`);

    socket.onopen = () => {
        connectDiv.hidden = true;
        chatDiv.hidden = false;
        addMessage("Connected to " + ip + ":" + port, "system");
        messageInput.focus();
    };

    socket.onmessage = (event) => addMessage(event.data);

    socket.onerror = () => {
        statusText.textContent = "Could not connect.";
    };

    socket.onclose = () => addMessage("Disconnected", "system");
});

document.getElementById("sendForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const text = messageInput.value.trim();
    if (!text || socket.readyState !== WebSocket.OPEN) return;
    socket.send(text);
    addMessage(text, "mine");
    messageInput.value = "";
});
