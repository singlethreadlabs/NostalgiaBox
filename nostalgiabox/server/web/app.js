"use strict";

const player = document.querySelector("#player");
const bug = document.querySelector("#channel-bug");
const numberLabel = document.querySelector("#channel-number");
const nameLabel = document.querySelector("#channel-name");
const programLabel = document.querySelector("#program-title");
const errorBox = document.querySelector("#error");
const staticLayer = document.querySelector("#static");
const channelInput = document.querySelector("#channel-input");

let lineup = [];
let currentIndex = 0;
let activeSession = null;
let tuneGeneration = 0;
let bugTimer = null;
let enteredDigits = "";
let digitTimer = null;

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `${response.status} ${response.statusText}`);
  }
  return response.status === 204 ? null : response.json();
}

function showBug(channel, program) {
  numberLabel.textContent = `CH ${String(channel.number).padStart(2, "0")}`;
  nameLabel.textContent = channel.name.toUpperCase();
  programLabel.textContent = program.title;
  bug.classList.add("visible");
  clearTimeout(bugTimer);
  bugTimer = setTimeout(() => bug.classList.remove("visible"), 4000);
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
}

async function releaseSession(sessionId) {
  if (!sessionId) return;
  await fetch(`/api/v1/playback-sessions/${sessionId}`, { method: "DELETE" });
}

async function tune(channelNumber) {
  const generation = ++tuneGeneration;
  const channel = lineup.find((entry) => entry.number === channelNumber);
  if (channel) {
    currentIndex = lineup.indexOf(channel);
    channelInput.value = channelNumber;
  }
  errorBox.hidden = true;
  staticLayer.classList.remove("active");
  void staticLayer.offsetWidth;
  staticLayer.classList.add("active");

  try {
    const session = await api("/api/v1/playback-sessions", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ channel_number: channelNumber }),
    });
    if (generation !== tuneGeneration) {
      await releaseSession(session.id);
      return;
    }
    const previous = activeSession;
    activeSession = session.id;
    player.src = session.browser_url;
    await player.play().catch(() => {
      showError("Your browser blocked autoplay. Press Play to start the channel.");
    });
    if (generation !== tuneGeneration) {
      await releaseSession(session.id);
      return;
    }
    if (channel) {
      channel.program = session.program;
      showBug(channel, session.program);
    }
    await releaseSession(previous);
  } catch (error) {
    showError(`Unable to tune channel ${channelNumber}: ${error.message}`);
  }
}

function changeChannel(delta) {
  if (!lineup.length) return;
  currentIndex = (currentIndex + delta + lineup.length) % lineup.length;
  void tune(lineup[currentIndex].number);
}

async function start() {
  try {
    const response = await api("/api/v1/channels");
    lineup = response.channels.map((entry) => ({
      ...entry.channel,
      program: entry.program,
    }));
    currentIndex = Math.max(
      0,
      lineup.findIndex((channel) => channel.number === response.start_channel),
    );
    await tune(lineup[currentIndex].number);
  } catch (error) {
    showError(`NostalgiaBox could not start: ${error.message}`);
  }
}

document.querySelector("#up").addEventListener("click", () => changeChannel(1));
document.querySelector("#down").addEventListener("click", () => changeChannel(-1));
document.querySelector("#play").addEventListener("click", async () => {
  if (player.paused) {
    await player.play();
  } else {
    player.pause();
  }
});
player.addEventListener("playing", () => {
  document.querySelector("#play").textContent = "Pause";
  errorBox.hidden = true;
});
player.addEventListener("pause", () => {
  document.querySelector("#play").textContent = "Play";
});
player.addEventListener("ended", () => {
  if (lineup[currentIndex]) void tune(lineup[currentIndex].number);
});
document.querySelector("#mute").addEventListener("click", (event) => {
  player.muted = !player.muted;
  event.currentTarget.textContent = player.muted ? "Unmute" : "Mute";
});
document.querySelector("#fullscreen").addEventListener("click", () => {
  void document.querySelector(".television").requestFullscreen();
});
document.querySelector("#channel-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const number = Number.parseInt(channelInput.value, 10);
  if (lineup.some((channel) => channel.number === number)) {
    void tune(number);
  } else {
    showError(`Channel ${channelInput.value} is not in the lineup.`);
  }
});

document.addEventListener("keydown", (event) => {
  if (event.target === channelInput) return;
  if (event.key === "ArrowUp") changeChannel(1);
  if (event.key === "ArrowDown") changeChannel(-1);
  if (event.key.toLowerCase() === "m") player.muted = !player.muted;
  if (event.key.toLowerCase() === "f") {
    void document.querySelector(".television").requestFullscreen();
  }
  if (/^[0-9]$/.test(event.key)) {
    enteredDigits = `${enteredDigits}${event.key}`.slice(-3);
    clearTimeout(digitTimer);
    digitTimer = setTimeout(() => {
      const number = Number.parseInt(enteredDigits, 10);
      enteredDigits = "";
      if (lineup.some((channel) => channel.number === number)) void tune(number);
    }, 700);
  }
});

window.addEventListener("pagehide", () => {
  if (activeSession) {
    void fetch(`/api/v1/playback-sessions/${activeSession}`, {
      method: "DELETE",
      keepalive: true,
    });
  }
});

void start();
