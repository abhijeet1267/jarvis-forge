package com.vantage.companion.data.local

import android.content.SharedPreferences

/**
 * In-memory [SharedPreferences] for JVM unit tests, which have no Android
 * runtime. Backs [SessionStore]'s injected constructor so the token/store
 * logic is tested for real without a Context or Keystore.
 */
class InMemorySharedPreferences : SharedPreferences {

    private val data = HashMap<String, Any?>()

    override fun getAll(): MutableMap<String, Any> =
        data.filterValues { it != null }.mapValues { it.value as Any }.toMutableMap()

    override fun getString(key: String?, defValue: String?): String? =
        (data[key] as? String) ?: defValue

    override fun getStringSet(key: String?, defValues: MutableSet<String>?): MutableSet<String>? =
        (data[key] as? MutableSet<String>) ?: defValues ?: mutableSetOf()

    override fun getInt(key: String?, defValue: Int): Int = (data[key] as? Int) ?: defValue

    override fun getLong(key: String?, defValue: Long): Long = (data[key] as? Long) ?: defValue

    override fun getFloat(key: String?, defValue: Float): Float = (data[key] as? Float) ?: defValue

    override fun getBoolean(key: String?, defValue: Boolean): Boolean =
        (data[key] as? Boolean) ?: defValue

    override fun contains(key: String?): Boolean = key != null && data.containsKey(key)

    override fun edit(): SharedPreferences.Editor = EditorImpl()

    override fun registerOnSharedPreferenceChangeListener(
        listener: SharedPreferences.OnSharedPreferenceChangeListener?,
    ) {}

    override fun unregisterOnSharedPreferenceChangeListener(
        listener: SharedPreferences.OnSharedPreferenceChangeListener?,
    ) {}

    private inner class EditorImpl : SharedPreferences.Editor {
        private val pending = HashMap<String, Any?>()
        private var toRemove = mutableSetOf<String>()
        private var clearAll = false

        override fun putString(key: String?, value: String?): SharedPreferences.Editor {
            put(key, value); return this
        }
        override fun putStringSet(key: String?, values: MutableSet<String>?): SharedPreferences.Editor {
            put(key, values); return this
        }
        override fun putInt(key: String?, value: Int): SharedPreferences.Editor {
            put(key, value); return this
        }
        override fun putLong(key: String?, value: Long): SharedPreferences.Editor {
            put(key, value); return this
        }
        override fun putFloat(key: String?, value: Float): SharedPreferences.Editor {
            put(key, value); return this
        }
        override fun putBoolean(key: String?, value: Boolean): SharedPreferences.Editor {
            put(key, value); return this
        }
        override fun remove(key: String?): SharedPreferences.Editor {
            if (key != null) toRemove.add(key); return this
        }
        override fun clear(): SharedPreferences.Editor {
            clearAll = true; return this
        }
        override fun commit(): Boolean {
            apply(); return true
        }
        override fun apply() {
            if (clearAll) data.clear()
            toRemove.forEach { data.remove(it) }
            data.putAll(pending)
            toRemove.clear()
            pending.clear()
            clearAll = false
        }

        private fun put(key: String?, value: Any?) {
            if (key == null) return
            pending[key] = value
            toRemove.remove(key)
        }
    }
}
