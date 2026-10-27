package com.carriez.flutter_hbb

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.IBinder
import android.os.Parcel
import android.util.Log

// TerraSmart Destek: writes this tablet's RustDesk ID into the Headwind MDM
// device's custom1 field through the Headwind launcher's plugin API, so the
// TerraSmart panel knows which ID each tablet has.
object TerraSmartMdm {
    private const val TAG = "TerraSmartMdm"
    private const val ACTION = "com.hmdm.action.Connect"
    private const val LAUNCHER = "com.hmdm.launcher"
    private const val DESCRIPTOR = "com.hmdm.IMdmApi"
    // IMdmApi's methods are numbered in declaration order and only ever appended;
    // setCustom(int, String) is the eighth (launcher API 1.1.3 and later).
    private const val SET_CUSTOM = IBinder.FIRST_CALL_TRANSACTION + 7
    private const val FIELD = 1

    fun reportId(context: Context, id: String) {
        if (id.isEmpty()) return
        val app = context.applicationContext
        val connection = object : ServiceConnection {
            override fun onServiceConnected(name: ComponentName?, service: IBinder?) {
                try {
                    service?.let { setCustom(it, id) }
                    Log.d(TAG, "RustDesk ID sent to Headwind as custom$FIELD")
                } catch (e: Exception) {
                    Log.w(TAG, "Headwind did not take the RustDesk ID: $e")
                } finally {
                    app.unbindService(this)
                }
            }

            override fun onServiceDisconnected(name: ComponentName?) {}
        }
        try {
            if (!app.bindService(Intent(ACTION).setPackage(LAUNCHER), connection, Context.BIND_AUTO_CREATE)) {
                runCatching { app.unbindService(connection) }
                Log.d(TAG, "No Headwind launcher to report the RustDesk ID to")
            }
        } catch (e: Exception) {
            Log.w(TAG, "Cannot reach the Headwind launcher: $e")
        }
    }

    private fun setCustom(binder: IBinder, value: String) {
        val data = Parcel.obtain()
        val reply = Parcel.obtain()
        try {
            data.writeInterfaceToken(DESCRIPTOR)
            data.writeInt(FIELD)
            data.writeString(value)
            binder.transact(SET_CUSTOM, data, reply, 0)
            reply.readException()
        } finally {
            data.recycle()
            reply.recycle()
        }
    }
}
