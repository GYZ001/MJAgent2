/**
 * 全站共用的音频播放单例（方案 5.4 节：「全站共用一个播放器，同一时刻只响一处」）。
 * 这是前端第一个 `<audio>`——真正的节点只在浏览器里懒创建、只创建一次并反复换
 * `src` 复用，不是每个 VoicePlayButton 各开一个。
 *
 * 播放器本体通过工厂函数注入（默认 `new Audio()`），不是在模块里硬编码：
 * vitest 全局跑在 node 环境（vite.config.ts::test.environment = 'node'，没有
 * jsdom），而 jsdom 即便挂载了也不真正实现 HTMLMediaElement.play/pause——测试
 * 用 __setVoiceAudioFactoryForTest 换一个纯 JS 假播放器，不依赖任何 DOM。
 */

export interface VoicePlayerHandle {
  play(): unknown;
  pause(): void;
  addEventListener(type: "ended", listener: () => void): void;
  removeEventListener(type: "ended", listener: () => void): void;
  src: string;
  currentTime: number;
  preload: string;
}

type Listener = () => void;

function createBrowserAudio(): VoicePlayerHandle {
  const audio = new Audio();
  audio.preload = "none";
  return audio as unknown as VoicePlayerHandle;
}

let audioFactory: () => VoicePlayerHandle = createBrowserAudio;
let handle: VoicePlayerHandle | null = null;
let activeId: string | null = null;
let endedListener: (() => void) | null = null;
const listeners = new Set<Listener>();

function notify(): void {
  listeners.forEach((listener) => listener());
}

/** 订阅"当前播放的是哪个 id"变化；返回取消订阅函数。 */
export function subscribeVoicePlayer(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function activeVoiceId(): string | null {
  return activeId;
}

function stopCurrent(): void {
  if (handle && endedListener) {
    handle.removeEventListener("ended", endedListener);
    endedListener = null;
  }
  handle?.pause();
  activeId = null;
}

/** 暂停当前播放（不切换到新的一处）。 */
export function pauseVoice(): void {
  if (!activeId) return;
  stopCurrent();
  notify();
}

/** 播放/暂停切换：对正在播放的 id 再次调用即暂停；对其他 id 调用会先暂停当前
 *  这一处、再切到新的一处——同一时刻只响一处。 */
export function toggleVoice(id: string, url: string): void {
  if (!id || !url) return;
  if (activeId === id) {
    pauseVoice();
    return;
  }
  stopCurrent();
  if (!handle) handle = audioFactory();
  handle.src = url;
  handle.currentTime = 0;
  const onEnded = () => {
    activeId = null;
    notify();
  };
  endedListener = onEnded;
  handle.addEventListener("ended", onEnded);
  activeId = id;
  notify();
  void handle.play();
}

/** 仅测试使用：注入不依赖真实 `<audio>` 的假播放器；传 null 恢复默认工厂。 */
export function __setVoiceAudioFactoryForTest(factory: (() => VoicePlayerHandle) | null): void {
  audioFactory = factory ?? createBrowserAudio;
}

/** 仅测试使用：清空模块级单例状态，避免同一测试文件里的用例互相污染。 */
export function __resetVoicePlayerForTest(): void {
  stopCurrent();
  handle = null;
  listeners.clear();
}
