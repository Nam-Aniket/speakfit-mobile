package com.aniket.speakfitmobile

import android.Manifest
import android.app.*
import android.app.job.*
import android.content.*
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.graphics.Color
import android.media.MediaRecorder
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.net.wifi.WifiManager
import android.os.*
import android.view.Gravity
import android.view.View
import android.view.WindowInsets
import android.widget.*
import java.io.File
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.URL
import java.text.SimpleDateFormat
import java.util.*
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

// Practice modes from the build plan; the code is the tag in the file name, the label is what you see.
val MODES = listOf("free" to "Free narration", "432" to "4/3/2", "smalltalk" to "Small talk with Claude",
    "retell" to "Read then retell", "interview" to "Interview stories")
const val PART_MS = 30 * 60 * 1000L   // a new file every 30 min: a crash loses at most one part, and the Mac starts on finished parts

fun recDir(c: Context) = File(c.getExternalFilesDir(null), "recordings").apply { mkdirs() }
fun prefs(c: Context): SharedPreferences = c.getSharedPreferences("sfm", Context.MODE_PRIVATE)
/** Debug APK only (make_apk.sh --debug): assets override part length and Mac address. Release APKs have no such assets. */
fun debugAsset(c: Context, name: String): String? = try { c.assets.open(name).bufferedReader().readText().trim() } catch (e: Exception) { null }
fun clock() = SimpleDateFormat("EEE HH:mm", Locale.UK).format(Date())

class MainActivity : Activity() {
    private lateinit var btn: Button
    private lateinit var status: TextView
    private lateinit var spinner: Spinner
    private val h = Handler(Looper.getMainLooper())
    private val tick = object : Runnable { override fun run() { refresh(); h.postDelayed(this, 1000) } }

    override fun onCreate(b: Bundle?) {
        super.onCreate(b)
        val d = resources.displayMetrics.density
        val pad = (24 * d).toInt()
        spinner = Spinner(this).apply {
            adapter = ArrayAdapter(this@MainActivity, android.R.layout.simple_spinner_dropdown_item, MODES.map { it.second })
            setSelection(prefs(this@MainActivity).getInt("mode", 0))
            onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
                override fun onItemSelected(p: AdapterView<*>?, v: View?, i: Int, id: Long) { prefs(this@MainActivity).edit().putInt("mode", i).apply() }
                override fun onNothingSelected(p: AdapterView<*>?) {}
            }
        }
        btn = Button(this).apply { textSize = 40f; setOnClickListener { toggle() } }
        status = TextView(this).apply { textSize = 16f; setPadding(0, pad / 2, 0, pad / 2) }
        val sync = Button(this).apply {
            text = "Send to Mac now"
            setOnClickListener { status.text = "Looking for the Mac..."; thread { Sync.runAndLog(this@MainActivity) } }
        }
        setContentView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL; setPadding(pad, pad, pad, pad); gravity = Gravity.CENTER_HORIZONTAL
            addView(TextView(context).apply { text = "Practice mode"; textSize = 14f })
            addView(spinner)
            addView(btn, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f).apply { topMargin = pad })
            addView(status)
            addView(sync)
            // target SDK 35 draws edge to edge: keep content clear of the status and navigation bars
            setOnApplyWindowInsetsListener { v, ins ->
                val b = ins.getInsets(WindowInsets.Type.systemBars())
                v.setPadding(pad, pad + b.top, pad, pad + b.bottom); ins
            }
        })
        Sync.schedule(this)
    }

    override fun onResume() { super.onResume(); h.post(tick) }
    override fun onPause() { super.onPause(); h.removeCallbacks(tick) }

    private fun refresh() {
        val t = RecorderService.startedAt
        val rec = t != 0L
        btn.text = if (rec) "STOP" else "START"
        btn.setBackgroundColor(if (rec) Color.rgb(198, 40, 40) else Color.rgb(46, 125, 50))
        btn.setTextColor(Color.WHITE)
        spinner.isEnabled = !rec
        val s = (System.currentTimeMillis() - t) / 1000
        status.text = buildString {
            append(if (rec) "Recording %d:%02d:%02d (screen can be off)\n".format(s / 3600, s / 60 % 60, s % 60) else "Ready. One tap starts a session.\n")
            append("Waiting to send to Mac: ${Sync.pending(this@MainActivity).size} file(s)\n")
            append("Last send: ${prefs(this@MainActivity).getString("last", "never")}")
        }
    }

    private fun toggle() {
        if (RecorderService.startedAt != 0L) { startService(Intent(this, RecorderService::class.java).setAction("stop")); return }
        val need = listOf(Manifest.permission.RECORD_AUDIO) +
            if (Build.VERSION.SDK_INT >= 33) listOf(Manifest.permission.POST_NOTIFICATIONS) else emptyList()
        val missing = need.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (missing.isNotEmpty()) { requestPermissions(missing.toTypedArray(), 1); return }
        startForegroundService(Intent(this, RecorderService::class.java).putExtra("mode", MODES[spinner.selectedItemPosition].first))
    }

    override fun onRequestPermissionsResult(code: Int, perms: Array<String>, res: IntArray) {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) toggle()
        else Toast.makeText(this, "SpeakFit needs the microphone to record.", Toast.LENGTH_LONG).show()
    }
}

class RecorderService : Service() {
    companion object { @Volatile var startedAt = 0L }   // 0 = not recording

    private var rec: MediaRecorder? = null
    private var cur: File? = null
    private var part = 0
    private var session = ""
    private var mode = "free"
    private var partMs = PART_MS
    private var wake: PowerManager.WakeLock? = null
    private val h = Handler(Looper.getMainLooper())
    private val rotate = object : Runnable { override fun run() { stopPart(); startPart(); h.postDelayed(this, partMs) } }

    override fun onBind(i: Intent?): IBinder? = null

    override fun onStartCommand(i: Intent?, flags: Int, id: Int): Int {
        if (i?.action == "stop") { stopAll(); return START_NOT_STICKY }
        if (startedAt != 0L) return START_NOT_STICKY
        mode = i?.getStringExtra("mode") ?: "free"
        session = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())
        startForeground(1, notification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        // ponytail: 10 h cap so a forgotten session cannot hold the CPU forever
        wake = getSystemService(PowerManager::class.java).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "sfm:rec").apply { acquire(10 * 3600 * 1000L) }
        startedAt = System.currentTimeMillis()
        startPart()
        partMs = debugAsset(this, "debug_part_ms")?.toLong() ?: PART_MS
        h.postDelayed(rotate, partMs)
        return START_NOT_STICKY
    }

    private fun startPart() {
        part++
        val f = File(recDir(this), "sfm_%s_%s_p%02d.m4a.part".format(session, mode, part))
        cur = f
        rec = (if (Build.VERSION.SDK_INT >= 31) MediaRecorder(this) else @Suppress("DEPRECATION") MediaRecorder()).apply {
            setAudioSource(MediaRecorder.AudioSource.MIC)
            setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            setAudioChannels(1)
            setAudioSamplingRate(44100)
            setAudioEncodingBitRate(48000)   // about 21 MB per hour
            setOutputFile(f.path)
            setOnErrorListener { _, _, _ -> h.post { stopPart(); startPart() } }
            prepare()
            start()
        }
    }

    private fun stopPart() {
        rec?.let { r ->
            try { r.stop() } catch (e: RuntimeException) { cur?.delete() }   // stop() throws when nothing was captured
            r.release()
        }
        rec = null
        cur?.takeIf { it.exists() }?.let { it.renameTo(File(it.path.removeSuffix(".part"))) }
        cur = null
        Sync.kick(this)
    }

    private fun stopAll() {
        h.removeCallbacks(rotate)
        stopPart()
        startedAt = 0L
        wake?.takeIf { it.isHeld }?.release()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onDestroy() { if (startedAt != 0L) stopAll(); super.onDestroy() }

    private fun notification(): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel("rec", "Recording", NotificationManager.IMPORTANCE_LOW))
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val stop = PendingIntent.getService(this, 1, Intent(this, RecorderService::class.java).setAction("stop"), PendingIntent.FLAG_IMMUTABLE)
        return Notification.Builder(this, "rec")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle("SpeakFit is recording")
            .setContentText(MODES.firstOrNull { it.first == mode }?.second ?: mode)
            .setOngoing(true).setContentIntent(open)
            .addAction(Notification.Action.Builder(null, "Stop", stop).build())
            .build()
    }
}

/** Finished recordings go to the Mac when the phone is on Wi-Fi and the Mac's SpeakFit service answers. */
object Sync {
    private const val TYPE = "_speakfit._tcp."

    fun pending(c: Context): List<File> = recDir(c).listFiles { f -> f.name.endsWith(".m4a") }.orEmpty().sortedBy { it.name }

    private fun job(c: Context, id: Int, periodic: Boolean) = JobInfo.Builder(id, ComponentName(c, SyncJob::class.java))
        .setRequiredNetworkType(JobInfo.NETWORK_TYPE_UNMETERED)
        .apply { if (periodic) setPeriodic(15 * 60 * 1000L).setPersisted(true) }
        .build()

    fun schedule(c: Context) { c.getSystemService(JobScheduler::class.java).schedule(job(c, 1, true)) }
    fun kick(c: Context) { c.getSystemService(JobScheduler::class.java).schedule(job(c, 2, false)) }

    /** Returns true when there is nothing left to send. */
    fun runAndLog(c: Context): Boolean {
        val (ok, msg) = try { run(c) } catch (e: Exception) { false to "error: ${e.javaClass.simpleName} ${e.message}" }
        prefs(c).edit().putString("last", "${clock()}: $msg").apply()
        return ok
    }

    @Synchronized
    private fun run(c: Context): Pair<Boolean, String> {
        val files = pending(c)
        if (files.isEmpty()) return true to "nothing new to send"
        val token = c.assets.open("token").bufferedReader().readText().trim()
        val host = findMac(c) ?: return false to "Mac not found on this network (${files.size} waiting)"
        for (f in files) {
            val code = put(host, f, token)
            if (code != 200) return false to "Mac answered $code for ${f.name}"
            f.renameTo(File(f.path + ".sent"))   // kept on the phone as a local copy
        }
        return true to "sent ${files.size} file(s) to $host"
    }

    private fun put(host: String, f: File, token: String): Int {
        val u = URL("http://$host/upload/${f.name}").openConnection() as HttpURLConnection
        try {
            u.requestMethod = "PUT"; u.doOutput = true; u.setFixedLengthStreamingMode(f.length())
            u.setRequestProperty("X-SpeakFit-Token", token)
            u.connectTimeout = 5000; u.readTimeout = 120_000
            u.outputStream.use { out -> f.inputStream().use { it.copyTo(out) } }
            return u.responseCode
        } finally { u.disconnect() }
    }

    private fun ping(host: String) = try {
        val u = URL("http://$host/ping").openConnection() as HttpURLConnection
        u.connectTimeout = 3000; u.readTimeout = 3000
        u.responseCode == 200 && u.inputStream.bufferedReader().readText().trim() == "speakfit"
    } catch (e: Exception) { false }

    private fun findMac(c: Context): String? {
        debugAsset(c, "debug_host")?.let { return it }
        prefs(c).getString("host", null)?.let { if (ping(it)) return it }
        val found = discover(c) ?: return null
        prefs(c).edit().putString("host", found).apply()
        return found
    }

    /** DNS-SD lookup for the Mac's "_speakfit._tcp" service, so a changed Mac IP address does not break sync. */
    private fun discover(c: Context): String? {
        val nsd = c.getSystemService(NsdManager::class.java)
        val lock = c.getSystemService(WifiManager::class.java).createMulticastLock("sfm").apply { acquire() }
        val done = CountDownLatch(1)
        var result: String? = null
        val listener = object : NsdManager.DiscoveryListener {
            override fun onServiceFound(s: NsdServiceInfo) {
                @Suppress("DEPRECATION")
                nsd.resolveService(s, object : NsdManager.ResolveListener {
                    override fun onServiceResolved(r: NsdServiceInfo) {
                        val a = r.host
                        if (a is Inet4Address && result == null) { result = "${a.hostAddress}:${r.port}"; done.countDown() }
                    }
                    override fun onResolveFailed(r: NsdServiceInfo, e: Int) {}
                })
            }
            override fun onStartDiscoveryFailed(t: String, e: Int) { done.countDown() }
            override fun onStopDiscoveryFailed(t: String, e: Int) {}
            override fun onDiscoveryStarted(t: String) {}
            override fun onDiscoveryStopped(t: String) {}
            override fun onServiceLost(s: NsdServiceInfo) {}
        }
        try {
            nsd.discoverServices(TYPE, NsdManager.PROTOCOL_DNS_SD, listener)
            done.await(15, TimeUnit.SECONDS)
            try { nsd.stopServiceDiscovery(listener) } catch (e: Exception) {}
        } finally { lock.release() }
        return result
    }
}

class SyncJob : JobService() {
    override fun onStartJob(p: JobParameters): Boolean {
        thread { jobFinished(p, !Sync.runAndLog(this)) }   // reschedule with backoff when the Mac was not reachable
        return true
    }
    override fun onStopJob(p: JobParameters) = true
}
