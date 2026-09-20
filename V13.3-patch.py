from pathlib import Path
import re

MAIN = Path("app/src/main/java/com/hansom/arzhv13/MainActivity.kt")
HYBRID = Path("app/src/main/java/com/hansom/arzhv13/HybridSpeechRecognizer.kt")
GRADLE = Path("app/build.gradle")
MANIFEST = Path("app/src/main/AndroidManifest.xml")
VOICE = Path("app/src/main/java/com/hansom/arzhv13/LocalZipVoiceEngine.kt")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly 1 anchor, found {count}")
    return text.replace(old, new, 1)


hybrid_code = r'''package com.hansom.arzhv132

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer

class HybridSpeechRecognizer(
    private val context: Context,
    private val listener: Listener,
) {
    interface Listener {
        fun onState(text: String)
        fun onLanguage(localeTag: String)
        fun onPartial(text: String, latencyMs: Long)
        fun onFinal(text: String, latencyMs: Long, confidence: Float)
        fun onRms(rmsDb: Float)
        fun onError(code: Int, message: String)
        fun onPermanentFailure(message: String)
    }

    companion object {
        private val LANGUAGE_ORDER = listOf(
            "ar-KW", "ar-SA", "ar-AE", "ar-IQ", "ar-EG", "ar"
        )
        private const val NETWORK_FAILURE_LIMIT = 3
        private const val BUSY_REBUILD_LIMIT = 2
    }

    private val main = Handler(Looper.getMainLooper())
    private var recognizer: SpeechRecognizer? = null
    private var desiredRunning = false
    private var pausedForTts = false
    private var sessionActive = false
    private var speechActive = false
    private var awaitingFinal = false
    private var speechStartedAt = 0L
    private var endSpeechAt = 0L
    private var generation = 0L
    private var recognizerToken = 0L
    private var languageIndex = 0
    private var consecutiveNetworkErrors = 0
    private var consecutiveBusyErrors = 0

    fun isAvailable(): Boolean = SpeechRecognizer.isRecognitionAvailable(context)
    fun currentLanguage(): String = LANGUAGE_ORDER[languageIndex]
    fun isSpeechActive(): Boolean = speechActive
    fun isAwaitingFinal(): Boolean = awaitingFinal

    fun startContinuous() {
        desiredRunning = true
        pausedForTts = false
        languageIndex = 0
        consecutiveNetworkErrors = 0
        consecutiveBusyErrors = 0
        if (!isAvailable()) {
            permanentFailure("手机没有可用的 Android 语音识别服务")
            return
        }
        if (!ensureRecognizer()) {
            permanentFailure("Android 语音识别服务创建失败")
            return
        }
        scheduleStart(0L)
    }

    fun stopContinuous() {
        desiredRunning = false
        pausedForTts = false
        generation++
        sessionActive = false
        speechActive = false
        awaitingFinal = false
        destroyRecognizer()
        listener.onState("■ Android 实时 ASR 已停止")
    }

    fun pauseForTts() {
        if (!desiredRunning) return
        pausedForTts = true
        generation++
        sessionActive = false
        speechActive = false
        awaitingFinal = false
        destroyRecognizer()
        listener.onState("🔇 播放中文，暂时暂停阿语识别")
    }

    fun resumeAfterTts(delayMs: Long = 250L) {
        if (!desiredRunning) return
        pausedForTts = false
        scheduleStart(delayMs)
    }

    fun release() {
        desiredRunning = false
        pausedForTts = false
        generation++
        sessionActive = false
        speechActive = false
        awaitingFinal = false
        destroyRecognizer()
    }

    private fun ensureRecognizer(): Boolean {
        if (recognizer != null) return true
        if (!isAvailable()) return false
        return try {
            val instanceToken = ++recognizerToken
            val r = SpeechRecognizer.createSpeechRecognizer(context)
            recognizer = r
            r.setRecognitionListener(object : RecognitionListener {
                private fun valid(): Boolean =
                    instanceToken == recognizerToken && recognizer === r

                override fun onReadyForSpeech(params: Bundle?) {
                    if (!valid() || !desiredRunning || pausedForTts) return
                    listener.onState("🎙 请说阿拉伯语 · ${currentLanguage()}")
                }

                override fun onBeginningOfSpeech() {
                    if (!valid()) return
                    speechActive = true
                    awaitingFinal = false
                    speechStartedAt = System.currentTimeMillis()
                    listener.onState("● 正在识别 · ${currentLanguage()}")
                }

                override fun onRmsChanged(rmsdB: Float) {
                    if (!valid() || !desiredRunning || pausedForTts) return
                    listener.onRms(rmsdB)
                }

                override fun onBufferReceived(buffer: ByteArray?) {}

                override fun onEndOfSpeech() {
                    if (!valid()) return
                    speechActive = false
                    awaitingFinal = true
                    endSpeechAt = System.currentTimeMillis()
                    listener.onState("… 正在整理整句")
                }

                override fun onError(error: Int) {
                    if (!valid()) return
                    sessionActive = false
                    speechActive = false
                    awaitingFinal = false
                    if (!desiredRunning || pausedForTts) return

                    listener.onError(error, errorMessage(error))

                    if (
                        error == SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS ||
                        error == SpeechRecognizer.ERROR_CLIENT
                    ) {
                        permanentFailure(errorMessage(error))
                        return
                    }

                    if (
                        error == SpeechRecognizer.ERROR_LANGUAGE_NOT_SUPPORTED ||
                        error == SpeechRecognizer.ERROR_LANGUAGE_UNAVAILABLE
                    ) {
                        if (languageIndex < LANGUAGE_ORDER.lastIndex) {
                            languageIndex++
                            listener.onLanguage(currentLanguage())
                            listener.onState("↪ 切换阿语地区：${currentLanguage()}")
                            scheduleStart(250L)
                        } else {
                            permanentFailure("手机语音服务不支持当前阿拉伯语地区")
                        }
                        return
                    }

                    if (
                        error == SpeechRecognizer.ERROR_NETWORK ||
                        error == SpeechRecognizer.ERROR_NETWORK_TIMEOUT ||
                        error == SpeechRecognizer.ERROR_TOO_MANY_REQUESTS
                    ) {
                        consecutiveNetworkErrors++
                        if (consecutiveNetworkErrors >= NETWORK_FAILURE_LIMIT) {
                            permanentFailure("Android ASR 连续网络失败，切换备用识别")
                            return
                        }
                        val delay = if (error == SpeechRecognizer.ERROR_TOO_MANY_REQUESTS) 5000L else 1400L
                        scheduleStart(delay)
                        return
                    }

                    if (
                        error == SpeechRecognizer.ERROR_RECOGNIZER_BUSY ||
                        error == SpeechRecognizer.ERROR_SERVER ||
                        error == SpeechRecognizer.ERROR_SERVER_DISCONNECTED
                    ) {
                        consecutiveBusyErrors++
                        if (consecutiveBusyErrors >= BUSY_REBUILD_LIMIT) {
                            destroyRecognizer()
                            consecutiveBusyErrors = 0
                        }
                        scheduleStart(850L)
                        return
                    }

                    val delay = when (error) {
                        SpeechRecognizer.ERROR_NO_MATCH,
                        SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> 180L
                        SpeechRecognizer.ERROR_AUDIO -> 700L
                        else -> 600L
                    }
                    scheduleStart(delay)
                }

                override fun onResults(results: Bundle?) {
                    if (!valid()) return
                    sessionActive = false
                    speechActive = false
                    val now = System.currentTimeMillis()
                    val texts = results
                        ?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                        .orEmpty()
                    val text = texts.firstOrNull().orEmpty().trim()
                    val scores = results?.getFloatArray(SpeechRecognizer.CONFIDENCE_SCORES)
                    val confidence = scores?.firstOrNull() ?: -1f
                    val latency = if (awaitingFinal && endSpeechAt > 0L) {
                        (now - endSpeechAt).coerceAtLeast(0L)
                    } else {
                        0L
                    }
                    awaitingFinal = false
                    consecutiveNetworkErrors = 0
                    consecutiveBusyErrors = 0

                    if (desiredRunning && !pausedForTts && text.isNotBlank()) {
                        listener.onFinal(text, latency, confidence)
                    }
                    if (desiredRunning && !pausedForTts) scheduleStart(220L)
                }

                override fun onPartialResults(partialResults: Bundle?) {
                    if (!valid() || !desiredRunning || pausedForTts) return
                    val text = partialResults
                        ?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                        ?.firstOrNull()
                        .orEmpty()
                        .trim()
                    if (text.isNotBlank()) {
                        val now = System.currentTimeMillis()
                        val latency = if (speechStartedAt > 0L) {
                            (now - speechStartedAt).coerceAtLeast(0L)
                        } else {
                            0L
                        }
                        listener.onPartial(text, latency)
                    }
                }

                override fun onEvent(eventType: Int, params: Bundle?) {}
            })
            true
        } catch (t: Throwable) {
            recognizer = null
            listener.onError(-2, t.message ?: t::class.java.simpleName)
            false
        }
    }

    private fun destroyRecognizer() {
        val r = recognizer
        recognizer = null
        recognizerToken++
        runCatching { r?.cancel() }
        runCatching { r?.destroy() }
    }

    private fun scheduleStart(delayMs: Long) {
        val token = ++generation
        main.postDelayed({
            if (token != generation) return@postDelayed
            if (!desiredRunning || pausedForTts || sessionActive) return@postDelayed
            if (!ensureRecognizer()) {
                permanentFailure("Android 语音识别服务不可用")
                return@postDelayed
            }
            val r = recognizer ?: run {
                permanentFailure("Android 语音识别服务不可用")
                return@postDelayed
            }

            val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
                putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                putExtra(RecognizerIntent.EXTRA_LANGUAGE, currentLanguage())
                putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
                putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 3)
                putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, false)
            }

            try {
                sessionActive = true
                speechActive = false
                awaitingFinal = false
                speechStartedAt = 0L
                endSpeechAt = 0L
                listener.onLanguage(currentLanguage())
                r.startListening(intent)
            } catch (t: Throwable) {
                sessionActive = false
                listener.onError(-2, t.message ?: t::class.java.simpleName)
                destroyRecognizer()
                if (desiredRunning && !pausedForTts) scheduleStart(900L)
            }
        }, delayMs)
    }

    private fun permanentFailure(message: String) {
        desiredRunning = false
        pausedForTts = false
        generation++
        sessionActive = false
        speechActive = false
        awaitingFinal = false
        destroyRecognizer()
        listener.onPermanentFailure(message)
    }

    private fun errorMessage(code: Int): String = when (code) {
        SpeechRecognizer.ERROR_AUDIO -> "录音错误"
        SpeechRecognizer.ERROR_CLIENT -> "识别客户端错误"
        SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> "麦克风权限不足"
        SpeechRecognizer.ERROR_NETWORK -> "网络错误"
        SpeechRecognizer.ERROR_NETWORK_TIMEOUT -> "网络超时"
        SpeechRecognizer.ERROR_NO_MATCH -> "本句未识别到有效文字"
        SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> "识别器忙"
        SpeechRecognizer.ERROR_SERVER -> "识别服务错误"
        SpeechRecognizer.ERROR_SERVER_DISCONNECTED -> "识别服务断开"
        SpeechRecognizer.ERROR_TOO_MANY_REQUESTS -> "识别请求过于频繁"
        SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> "等待语音超时"
        SpeechRecognizer.ERROR_LANGUAGE_NOT_SUPPORTED -> "当前阿语地区语言不支持"
        SpeechRecognizer.ERROR_LANGUAGE_UNAVAILABLE -> "当前阿语地区语言暂不可用"
        else -> "识别错误 $code"
    }
}
'''
HYBRID.write_text(hybrid_code, encoding="utf-8")

voice = VOICE.read_text(encoding="utf-8")
voice = replace_once(
    voice,
    '              fun isRecording(): Boolean = recording.get()\n',
    '              fun isRecording(): Boolean = recording.get()\n'
    '              fun isSpeaking(): Boolean = speaking.get()\n',
    "voice isSpeaking",
)
VOICE.write_text(voice, encoding="utf-8")

g = GRADLE.read_text(encoding="utf-8")
g = replace_once(g, "versionCode 132", "versionCode 133", "versionCode")
g = replace_once(g, "versionName '13.2'", "versionName '13.3'", "versionName")
GRADLE.write_text(g, encoding="utf-8")

m = MANIFEST.read_text(encoding="utf-8")
m = m.replace("阿语方言同传 V13.2", "阿语方言同传 V13.3")
anchor = '    <uses-permission android:name="android.permission.INTERNET" />\n'
queries = (
    '    <queries>\n'
    '        <intent>\n'
    '            <action android:name="android.speech.RecognitionService" />\n'
    '        </intent>\n'
    '    </queries>\n'
)
if queries not in m:
    if anchor not in m:
        raise SystemExit("manifest internet permission anchor missing")
    m = m.replace(anchor, anchor + queries, 1)
MANIFEST.write_text(m, encoding="utf-8")

s = MAIN.read_text(encoding="utf-8")

# The V13.2 source is generated from a YAML heredoc. The patch anchors below
# intentionally mirror the workflow-source indentation (10 extra spaces).
# Pad the generated Kotlin temporarily, apply exact anchors, then unpad before writing.
_had_final_newline = s.endswith("\n")
s = "\n".join("          " + line for line in s.splitlines())
if _had_final_newline:
    s += "\n"

# V13.3 no longer requires Whisper before real-time interpretation.
# Remove the old V13.2 first-run dialog that implied the 474 MB model was mandatory.
s, dialog_count = re.subn(
    r"\n\s+handler\.postDelayed\(\{\n\s+if \(!asr\.isModelReady\(\)\) \{.*?\n\s+\}, 650\)\n",
    "\n",
    s,
    count=1,
    flags=re.S,
)
if dialog_count != 1:
    raise SystemExit(f"first-run dialog: expected 1 block, found {dialog_count}")

s = replace_once(
    s,
    '              private lateinit var asr: DialectAsrEngine\n',
    '              private lateinit var asr: DialectAsrEngine\n'
    '              private lateinit var hybridAsr: HybridSpeechRecognizer\n'
    '              private var hybridListening = false\n'
    '              private var lastAsrMetric = "⚡ Android ASR：等待"\n'
    '              private var lastHybridFinal = ""\n'
    '              private var lastHybridFinalAt = 0L\n'
    '              private var whisperFallbackPending = false\n'
    '              private var translationEpoch = 1L\n'
    '              private var speechRetryScheduled = false\n',
    "hybrid fields",
)

s = replace_once(
    s,
    '              private data class TranslationJob(val raw: String, val msa: String)\n',
    '              private data class TranslationJob(\n'
    '                  val raw: String,\n'
    '                  val msa: String,\n'
    '                  val createdAt: Long = System.currentTimeMillis(),\n'
    '                  val epoch: Long,\n'
    '              )\n',
    "translation telemetry",
)

s = replace_once(
    s,
    '                  initAsr()\n',
    '                  initAsr()\n                  initHybridAsr()\n',
    "init hybrid",
)

s = replace_once(
    s,
    '''                      override fun onModelReady(ready: Boolean) {
                          asrModelBtn.text = if (ready) {
                              "✅ 方言识别模型：已就绪"
                          } else {
                              "① 下载阿语方言识别模型（首次约 474 MB）"
                          }
                          startBtn.isEnabled = ready
                      }
''',
    '''                      override fun onModelReady(ready: Boolean) {
                          asrModelBtn.text = if (ready) {
                              "✅ Whisper备用模型：已就绪"
                          } else {
                              "下载 Whisper 备用模型（约 474 MB，可选）"
                          }
                          if (whisperFallbackPending) {
                              if (ready) {
                                  whisperFallbackPending = false
                                  startBtn.isEnabled = true
                                  startBtn.text = "■ 停止Whisper备用识别"
                                  asr.start()
                              } else {
                                  whisperFallbackPending = false
                                  startBtn.isEnabled = true
                                  startBtn.text = "🎙 开始实时同传"
                              }
                          }
                      }
''',
    "optional whisper model",
)

s = replace_once(
    s,
    '''                      override fun onListeningChanged(listening: Boolean) {
                          startBtn.text = if (listening) "■ 停止同传" else "🎙 开始方言同传"
                      }
''',
    '''                      override fun onListeningChanged(listening: Boolean) {
                          if (!hybridListening && !whisperFallbackPending) {
                              startBtn.isEnabled = true
                              startBtn.text = if (listening) {
                                  "■ 停止Whisper备用识别"
                              } else {
                                  "🎙 开始实时同传"
                              }
                              pipelineStatus.text = if (listening) {
                                  "🧠 正在使用Whisper备用识别"
                              } else {
                                  "■ Whisper备用识别已停止"
                              }
                          }
                      }
''',
    "whisper state callback",
)

s = replace_once(
    s,
    '''                      override fun onDecodeTelemetry(audioMs: Int, latencyMs: Long, chars: Int) {
                          decodeStatus.text = "🧠 Whisper：${audioMs}ms 音频 → ${chars}字   推理 ${latencyMs}ms"
                      }
''',
    '''                      override fun onDecodeTelemetry(audioMs: Int, latencyMs: Long, chars: Int) {
                          if (!hybridListening) {
                              lastAsrMetric =
                                  "🧠 Whisper备用 · ${audioMs}ms → ${chars}字 · ${latencyMs}ms"
                              decodeStatus.text = lastAsrMetric
                          }
                      }
''',
    "whisper telemetry",
)

s = replace_once(
    s,
    '''                      override fun onPartial(text: String) {
                          partialRaw = text
''',
    '''                      override fun onPartial(text: String) {
                          if (hybridListening) return
                          partialRaw = text
''',
    "ignore stale Whisper partials",
)
s = replace_once(
    s,
    '''                      override fun onStable(text: String) {
                          if (text.isBlank()) return
''',
    '''                      override fun onStable(text: String) {
                          if (hybridListening) return
                          if (text.isBlank()) return
''',
    "ignore stale Whisper stable",
)
s = replace_once(
    s,
    '''                      override fun onFinal(text: String) {
                          partialRaw = ""
''',
    '''                      override fun onFinal(text: String) {
                          if (hybridListening) return
                          partialRaw = ""
''',
    "ignore stale Whisper final",
)

hybrid_methods = r'''
              private fun initHybridAsr() {
                  hybridAsr = HybridSpeechRecognizer(
                      this,
                      object : HybridSpeechRecognizer.Listener {
                          override fun onState(text: String) {
                              if (hybridListening) pipelineStatus.text = text
                          }

                          override fun onLanguage(localeTag: String) {
                              if (hybridListening) {
                                  pipelineStatus.text = "🎙 Android实时ASR · $localeTag · 连续自动续接"
                              }
                          }

                          override fun onPartial(text: String, latencyMs: Long) {
                              if (!hybridListening) return
                              partialRaw = text
                              lastAsrMetric =
                                  "⚡ Android ASR · ${hybridAsr.currentLanguage()} · partial ${latencyMs}ms"
                              decodeStatus.text = lastAsrMetric
                              dialectStatus.text = "当前判断：" + ArabicDialectNormalizer.detect(text)
                              renderRaw()
                          }

                          override fun onFinal(text: String, latencyMs: Long, confidence: Float) {
                              if (!hybridListening || text.isBlank()) return
                              val now = System.currentTimeMillis()
                              if (text == lastHybridFinal && now - lastHybridFinalAt < 1800L) return
                              lastHybridFinal = text
                              lastHybridFinalAt = now

                              partialRaw = ""
                              if (rawHistory.isNotEmpty()) rawHistory.append("\n\n")
                              rawHistory.append(text.trim())

                              val normalized = ArabicDialectNormalizer.normalize(text)
                              if (normalized.msa.isNotBlank()) {
                                  if (msaHistory.isNotEmpty()) msaHistory.append("\n")
                                  msaHistory.append(normalized.msa)
                                  msaText.text = msaHistory.toString()
                              }

                              val confidenceText = if (confidence >= 0f) {
                                  " · conf ${(confidence * 100).toInt()}%"
                              } else ""
                              lastAsrMetric =
                                  "⚡ Android ASR · ${hybridAsr.currentLanguage()} · final ${latencyMs}ms$confidenceText"
                              decodeStatus.text = lastAsrMetric
                              dialectStatus.text = "最近方言：" + ArabicDialectNormalizer.detect(text)

                              enqueueTranslation(
                                  TranslationJob(
                                      raw = text.trim(),
                                      msa = normalized.msa,
                                      epoch = translationEpoch,
                                  )
                              )
                              renderRaw()
                          }

                          override fun onRms(rmsDb: Float) {
                              if (!hybridListening) return
                              micStatus.text =
                                  "🎤 Android ASR · ${hybridAsr.currentLanguage()} · RMS ${"%.1f".format(rmsDb)} dB"
                          }

                          override fun onError(code: Int, message: String) {
                              if (!hybridListening) return
                              pipelineStatus.text = "⚠ Android ASR：$message · 自动恢复中"
                          }

                          override fun onPermanentFailure(message: String) {
                              if (!hybridListening) return
                              hybridListening = false
                              startWhisperFallback("Android ASR：$message")
                          }
                      },
                  )

                  val available = hybridAsr.isAvailable()
                  startBtn.isEnabled = true
                  pipelineStatus.text = when {
                      available -> "✅ V13.3 实时识别已就绪 · Android ASR主链 · Whisper备用"
                      asr.isModelReady() -> "⚠ Android ASR不可用 · Whisper备用模型已下载"
                      else -> "⚠ Android ASR不可用 · 可下载Whisper备用模型"
                  }
              }

              private fun startHybridAsr() {
                  if (
                      checkSelfPermission(Manifest.permission.RECORD_AUDIO) !=
                      PackageManager.PERMISSION_GRANTED
                  ) {
                      requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 1301)
                      return
                  }

                  whisperFallbackPending = false
                  if (::hybridAsr.isInitialized && hybridAsr.isAvailable()) {
                      if (asr.isListening()) asr.stop()
                      hybridListening = true
                      startBtn.text = "■ 停止同传"
                      pipelineStatus.text = "🎙 Android实时ASR · ${hybridAsr.currentLanguage()}"
                      hybridAsr.startContinuous()
                  } else {
                      startWhisperFallback("Android ASR不可用")
                  }
              }

              private fun startWhisperFallback(reason: String) {
                  hybridListening = false
                  if (::hybridAsr.isInitialized) hybridAsr.stopContinuous()
                  when {
                      asr.isReady() -> {
                          whisperFallbackPending = false
                          startBtn.isEnabled = true
                          startBtn.text = "■ 停止Whisper备用识别"
                          pipelineStatus.text = "🧠 $reason · 已切换Whisper备用"
                          asr.start()
                      }
                      asr.isModelReady() -> {
                          whisperFallbackPending = true
                          startBtn.isEnabled = false
                          startBtn.text = "● 正在加载Whisper备用模型…"
                          pipelineStatus.text = "🧠 $reason · 正在按需加载Whisper备用模型"
                          asr.initializeIfPossible()
                      }
                      else -> {
                          whisperFallbackPending = false
                          startBtn.isEnabled = true
                          startBtn.text = "🎙 开始实时同传"
                          pipelineStatus.text = "⚠ $reason · Whisper备用模型未下载"
                          Toast.makeText(
                              this,
                              "Android实时识别不可用，请下载Whisper备用模型",
                              Toast.LENGTH_LONG,
                          ).show()
                      }
                  }
              }

              private fun stopHybridAsr() {
                  translationEpoch++
                  whisperFallbackPending = false
                  hybridListening = false
                  if (::hybridAsr.isInitialized) hybridAsr.stopContinuous()
                  if (asr.isListening()) asr.stop()
                  translationQueue.clear()
                  speechQueue.clear()
                  systemTts?.stop()
                  if (!::localVoice.isInitialized || !localVoice.isSpeaking()) {
                      speechBusy.set(false)
                  }
                  startBtn.isEnabled = true
                  startBtn.text = "🎙 开始实时同传"
                  pipelineStatus.text = "■ 同传已停止"
              }

              private fun pauseHybridForPlayback() {
                  if (hybridListening && ::hybridAsr.isInitialized) {
                      hybridAsr.pauseForTts()
                  }
              }

              private fun resumeHybridAfterPlayback() {
                  if (hybridListening && ::hybridAsr.isInitialized) {
                      hybridAsr.resumeAfterTts(250L)
                  }
              }

              private fun scheduleSpeechRetry() {
                  if (speechRetryScheduled) return
                  speechRetryScheduled = true
                  handler.postDelayed({
                      speechRetryScheduled = false
                      speakNext()
                  }, 120L)
              }

              private fun runTranslationDiagnostic() {
                  val raw = "شنو المطلوب من الشركة الحين؟"
                  val normalized = ArabicDialectNormalizer.normalize(raw)
                  partialRaw = ""
                  if (rawHistory.isNotEmpty()) rawHistory.append("\n\n")
                  rawHistory.append("🧪 " ).append(raw)
                  if (msaHistory.isNotEmpty()) msaHistory.append("\n")
                  msaHistory.append(normalized.msa)
                  msaText.text = msaHistory.toString()
                  renderRaw()
                  enqueueTranslation(
                      TranslationJob(
                          raw = raw,
                          msa = normalized.msa,
                          epoch = translationEpoch,
                      )
                  )
              }

'''
marker = '              private fun initTranslation() {\n'
if marker not in s:
    raise SystemExit("initTranslation insertion anchor missing")
s = s.replace(marker, hybrid_methods + marker, 1)

s = replace_once(
    s,
    '                  onlineTranslate(job.msa) { online ->\n',
    '                  onlineTranslate(job.raw) { online ->\n',
    "direct online dialect translation",
)
s = replace_once(
    s,
    '                          translator.translate(job.msa)\n',
    '                          translator.translate(job.msa.ifBlank { job.raw })\n',
    "ML Kit MSA fallback translation",
)
s = replace_once(
    s,
    '                  enqueueTranslation(TranslationJob(normalized.raw, normalized.msa))\n',
    '                  enqueueTranslation(TranslationJob(normalized.raw, normalized.msa, epoch = translationEpoch))\n',
    "Whisper translation epoch",
)

s = replace_once(
    s,
    '                  while (translationQueue.size >= 6) translationQueue.poll()\n',
    '                  while (translationQueue.size >= 1) translationQueue.poll()\n',
    "latest-only translation queue",
)
s = replace_once(
    s,
    '''                  zhText.text = zhHistory.toString()

                  if (autoSpeak && !chinese.startsWith("[")) {
''',
    '''                  zhText.text = zhHistory.toString()
                  val translateMs =
                      (System.currentTimeMillis() - job.createdAt).coerceAtLeast(0L)
                  decodeStatus.text =
                      "$lastAsrMetric · 翻译 ${translateMs}ms"

                  if (autoSpeak && !chinese.startsWith("[")) {
''',
    "translation telemetry",
)

s = replace_once(
    s,
    '''              private fun translateNext() {
                  val job = translationQueue.poll()
                  if (job == null) {
                      translating.set(false)
                      return
                  }

                  onlineTranslate(job.raw) { online ->
                      if (online.isNotBlank()) {
                          consumeChinese(job, correctDomainTerms(job, online))
                          translateNext()
                      } else if (translatorReady) {
                          translator.translate(job.msa.ifBlank { job.raw })
                              .addOnSuccessListener { local ->
                                  consumeChinese(job, correctDomainTerms(job, local))
                                  translateNext()
                              }
                              .addOnFailureListener {
                                  consumeChinese(job, "[暂时无法翻译]")
                                  translateNext()
                              }
                      } else {
                          prepareTranslationModel()
                          consumeChinese(job, "[网络翻译暂不可用]")
                          translateNext()
                      }
                  }
              }

              private fun consumeChinese(job: TranslationJob, chinese: String) {
                  if (chinese.isBlank()) return
                  if (zhHistory.isNotEmpty()) zhHistory.append("\n")
                  zhHistory.append(chinese.trim())
                  zhText.text = zhHistory.toString()
                  val translateMs =
                      (System.currentTimeMillis() - job.createdAt).coerceAtLeast(0L)
                  decodeStatus.text =
                      "$lastAsrMetric · 翻译 ${translateMs}ms"

                  if (autoSpeak && !chinese.startsWith("[")) {
                      enqueueSpeechText(chinese.trim())
                  }
              }
''',
    '''              private fun translateNext() {
                  val job = translationQueue.poll()
                  if (job == null) {
                      translating.set(false)
                      return
                  }

                  onlineTranslate(job.raw) { online ->
                      if (online.isNotBlank()) {
                          consumeChinese(job, correctDomainTerms(job, online), "Online")
                          translateNext()
                      } else if (translatorReady) {
                          translator.translate(job.msa.ifBlank { job.raw })
                              .addOnSuccessListener { local ->
                                  consumeChinese(job, correctDomainTerms(job, local), "ML Kit")
                                  translateNext()
                              }
                              .addOnFailureListener {
                                  consumeChinese(job, "[暂时无法翻译]", "Unavailable")
                                  translateNext()
                              }
                      } else {
                          prepareTranslationModel()
                          consumeChinese(job, "[网络翻译暂不可用]", "Unavailable")
                          translateNext()
                      }
                  }
              }

              private fun consumeChinese(job: TranslationJob, chinese: String, source: String) {
                  if (job.epoch != translationEpoch || chinese.isBlank()) return
                  if (zhHistory.isNotEmpty()) zhHistory.append("\n")
                  zhHistory.append(chinese.trim())
                  zhText.text = zhHistory.toString()
                  val translateMs =
                      (System.currentTimeMillis() - job.createdAt).coerceAtLeast(0L)
                  decodeStatus.text =
                      "$lastAsrMetric · 翻译[$source] ${translateMs}ms"

                  if (autoSpeak && !chinese.startsWith("[")) {
                      enqueueSpeechText(chinese.trim())
                  }
              }
''',
    "translation source and epoch",
)
s = replace_once(
    s,
    '''              private fun speakNext() {
                  if (!speechBusy.compareAndSet(false, true)) return
                  val text = speechQueue.poll()
                  if (text == null) {
                      speechBusy.set(false)
                      return
                  }
''',
    '''              private fun speakNext() {
                  if (!autoSpeak) {
                      speechQueue.clear()
                      return
                  }
                  if (
                      hybridListening &&
                      ::hybridAsr.isInitialized &&
                      (hybridAsr.isSpeechActive() || hybridAsr.isAwaitingFinal())
                  ) {
                      scheduleSpeechRetry()
                      return
                  }
                  if (!speechBusy.compareAndSet(false, true)) return
                  val text = speechQueue.poll()
                  if (text == null) {
                      speechBusy.set(false)
                      resumeHybridAfterPlayback()
                      return
                  }
                  pauseHybridForPlayback()
''',
    "pause ASR before TTS",
)
s = replace_once(
    s,
    '''                              @Deprecated("Deprecated in Java")
                              override fun onError(utteranceId: String?) {
                                  runOnUiThread { finishSpeech("⚠ 系统中文朗读失败") }
                              }
''',
    '''                              @Deprecated("Deprecated in Java")
                              override fun onError(utteranceId: String?) {
                                  runOnUiThread { finishSpeech("⚠ 系统中文朗读失败") }
                              }
                              override fun onStop(utteranceId: String?, interrupted: Boolean) {
                                  runOnUiThread { finishSpeech("") }
                              }
''',
    "system TTS onStop",
)

s = replace_once(
    s,
    '''              private fun finishSpeech(message: String) {
                  speechBusy.set(false)
                  if (message.isNotBlank()) voiceStatus.text = message
                  speakNext()
              }
''',
    '''              private fun finishSpeech(message: String) {
                  speechBusy.set(false)
                  if (message.isNotBlank()) voiceStatus.text = message
                  if (speechQueue.isEmpty()) {
                      resumeHybridAfterPlayback()
                  } else {
                      speakNext()
                  }
              }
''',
    "resume ASR after TTS",
)
s = replace_once(
    s,
    '''                          autoSpeak = !autoSpeak
                          getSharedPreferences("v132", MODE_PRIVATE)
                              .edit().putBoolean("auto_speak", autoSpeak).apply()
                          updateAutoSpeakLabel()
''',
    '''                          autoSpeak = !autoSpeak
                          getSharedPreferences("v132", MODE_PRIVATE)
                              .edit().putBoolean("auto_speak", autoSpeak).apply()
                          if (!autoSpeak) {
                              speechQueue.clear()
                              systemTts?.stop()
                          }
                          updateAutoSpeakLabel()
''',
    "auto-speak stop queued audio",
)
s = replace_once(
    s,
    '''              private fun clearAll() {
                  rawHistory.setLength(0)
''',
    '''              private fun clearAll() {
                  translationEpoch++
                  translationQueue.clear()
                  speechQueue.clear()
                  systemTts?.stop()
                  rawHistory.setLength(0)
''',
    "clear invalidates async work",
)
s = replace_once(
    s,
    '''                  root.addView(sectionLabel("方言原文（实时）"))
''',
    '''                  root.addView(Button(this).apply {
                      text = "🧪 测试翻译链路（不使用麦克风）"
                      isAllCaps = false
                      setOnClickListener { runTranslationDiagnostic() }
                  })

                  root.addView(sectionLabel("方言原文（实时）"))
''',
    "translation diagnostic button",
)

s = replace_once(
    s,
    '''                  if (asr.isListening()) {
                      asr.stop()
                      Toast.makeText(this, "已暂停同传，先录制本人声音", Toast.LENGTH_SHORT).show()
                  }
                  localVoice.toggleRecording()
''',
    '''                  if (hybridListening || asr.isListening()) {
                      stopHybridAsr()
                      Toast.makeText(
                          this,
                          "已暂停同传，先录制本人声音",
                          Toast.LENGTH_SHORT,
                      ).show()
                  }
                  localVoice.toggleRecording()
''',
    "voice recording stops ASR",
)

old_start = '''                  startBtn = Button(this).apply {
                      text = "🎙 开始方言同传"
                      textSize = 20f
                      isAllCaps = false
                      isEnabled = false
                      setOnClickListener {
                          if (asr.isListening()) asr.stop() else {
                              if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                                  requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 1301)
                              } else if (!asr.isReady()) {
                                  Toast.makeText(this@MainActivity, "请先等待方言模型加载完成", Toast.LENGTH_SHORT).show()
                                  asr.initializeIfPossible()
                              } else {
                                  asr.start()
                              }
                          }
                      }
                  }
'''
new_start = '''                  startBtn = Button(this).apply {
                      text = "🎙 开始实时同传"
                      textSize = 20f
                      isAllCaps = false
                      isEnabled = true
                      setOnClickListener {
                          if (hybridListening || asr.isListening()) {
                              stopHybridAsr()
                          } else {
                              startHybridAsr()
                          }
                      }
                  }
'''
s = replace_once(s, old_start, new_start, "hybrid start button")

s = replace_once(
    s,
    '''                  if (asr.isModelReady()) {
                      asrModelBtn.text = "✅ 方言识别模型：正在加载"
                  }
                  asr.initializeIfPossible()
              }
''',
    '''                  if (asr.isModelReady()) {
                      asrModelBtn.text = "✅ Whisper备用模型：已下载（按需加载）"
                  }
              }
''',
    "lazy Whisper startup",
)
s = replace_once(
    s,
    '''              override fun onResume() {
                  super.onResume()
                  if (::asr.isInitialized) asr.initializeIfPossible()
                  if (::localVoice.isInitialized) localVoice.initializeIfPossible()
              }
''',
    '''              override fun onResume() {
                  super.onResume()
                  if (::localVoice.isInitialized) localVoice.initializeIfPossible()
              }
''',
    "lazy Whisper resume",
)

s = replace_once(
    s,
    '''                          } else {
                              if (asr.isListening()) asr.stop()
                              asr.startMicSelfTest()
                          }
''',
    '''                          } else {
                              if (hybridListening || asr.isListening()) {
                                  stopHybridAsr()
                              }
                              asr.startMicSelfTest()
                          }
''',
    "mic self-test stops hybrid ASR",
)

s = replace_once(
    s,
    '''              override fun onDestroy() {
                  runCatching { if (::asr.isInitialized) asr.release() }
''',
    '''              override fun onDestroy() {
                  runCatching {
                      if (::hybridAsr.isInitialized) hybridAsr.release()
                  }
                  runCatching { if (::asr.isInitialized) asr.release() }
''',
    "release hybrid recognizer",
)

replacements = {
    "阿语方言 → 标准阿语 → 中文\\nV13.2 · Mic Diagnostic Interpreter":
        "阿语方言 → 中文\\nV13.3 · Hybrid Real-Time Interpreter",
    "先验证麦克风 PCM，再进入 Whisper 方言 ASR · 海湾/科威特/埃及/伊拉克/黎凡特":
        "Android实时ASR主链 · 方言原句直接翻中文 · MSA辅助显示 · Whisper备用",
    "● V13.2 启动中 · 建议先做麦克风自检":
        "● V13.3 启动中 · 正在检查Android实时识别",
    "① 下载阿语方言识别模型（首次约 474 MB）":
        "下载 Whisper 备用模型（约 474 MB，可选）",
    "🧠 Whisper：等待首个音频窗口":
        "⚡ Android ASR：等待语音",
    "标准阿拉伯语 MSA":
        "标准阿拉伯语 MSA（辅助显示）",
    "中文翻译（稳定片段即时朗读）":
        "中文翻译（方言原句直译）",
    "中文稳定片段会立即显示并朗读。":
        "整句中文会立即显示并朗读。",
    "你好，这是 V13.2 方言实时同传测试。中文稳定以后会立即用我的声音朗读。":
        "你好，这是 V13.3 混合实时同传测试。整句翻译后会立即用我的声音朗读。",
}
for old, new in replacements.items():
    s = s.replace(old, new)

# Restore normal Kotlin indentation after all exact workflow-style patches.
_lines = s.splitlines()
if any(line and not line.startswith("          ") for line in _lines):
    bad = [line for line in _lines if line and not line.startswith("          ")][:3]
    raise SystemExit(f"unexpected unpadded Kotlin lines before write: {bad}")
s = "\n".join(line[10:] if line.startswith("          ") else line for line in _lines)
if _had_final_newline:
    s += "\n"
MAIN.write_text(s, encoding="utf-8")

test = Path("app/src/test/java/com/hansom/arzhv132/V133HybridPolicyTest.kt")
test.parent.mkdir(parents=True, exist_ok=True)
test.write_text(
    '''package com.hansom.arzhv132

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class V133HybridPolicyTest {
    @Test fun dialectNormalizerKeepsOriginalRawSentence() {
        val input = "شنو المطلوب من الشركة الحين"
        val r = ArabicDialectNormalizer.normalize(input)
        assertEquals(input, r.raw)
        assertTrue(r.msa.isNotBlank())
    }

    @Test fun kuwaitDialectDetectionStillWorks() {
        val d = ArabicDialectNormalizer.detect("أبي أعرف شنو المطلوب الحين")
        assertTrue(d.contains("خليجي") || d.contains("كويتي"))
    }
}
''',
    encoding="utf-8",
)

main = MAIN.read_text(encoding="utf-8")
hybrid = HYBRID.read_text(encoding="utf-8")
gradle = GRADLE.read_text(encoding="utf-8")
manifest = MANIFEST.read_text(encoding="utf-8")
checks = {
    "version 13.3": "versionCode 133" in gradle and "versionName '13.3'" in gradle,
    "same package": "applicationId 'com.hansom.arzhv132'" in gradle,
    "SpeechRecognizer main": "SpeechRecognizer.createSpeechRecognizer" in hybrid,
    "ar-KW first": '"ar-KW", "ar-SA", "ar-AE", "ar-IQ", "ar-EG", "ar"' in hybrid,
    "partial results": "EXTRA_PARTIAL_RESULTS" in hybrid,
    "continuous restart": "scheduleStart(220L)" in hybrid,
    "stale recognizer isolation": "recognizerToken" in hybrid and "destroyRecognizer()" in hybrid,
    "awaiting final guard": "isAwaitingFinal()" in hybrid and "awaitingFinal" in hybrid,
    "network circuit breaker": "NETWORK_FAILURE_LIMIT" in hybrid,
    "default endpointer": "EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS" not in hybrid,
    "language fallback": "languageIndex++" in hybrid,
    "recognition service query": "android.speech.RecognitionService" in manifest,
    "direct online translation": "onlineTranslate(job.raw)" in main,
    "ML Kit MSA fallback": "translator.translate(job.msa.ifBlank { job.raw })" in main,
    "latest-only queue": "translationQueue.size >= 1" in main,
    "TTS pauses ASR": "pauseHybridForPlayback()" in main,
    "TTS resumes ASR": "hybridAsr.resumeAfterTts(250L)" in main,
    "TTS waits for final": "hybridAsr.isAwaitingFinal()" in main,
    "async epoch guard": "job.epoch != translationEpoch" in main,
    "translation diagnostic": "测试翻译链路（不使用麦克风）" in main,
    "lazy Whisper": "Whisper备用模型：已下载（按需加载）" in main,
    "Whisper fallback": "asr.start()" in main,
    "mic self-test": "asr.startMicSelfTest()" in main,
}
bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(("PASS " if v else "FAIL ") + k)
if bad:
    raise SystemExit("V13.3 patch self-check failed: " + ", ".join(bad))
