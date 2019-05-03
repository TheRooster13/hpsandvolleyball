/*
 * Copyright (C) 2015 Square, Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
/*
 * Modifications copyright (C) 2018 HP Development Company, L.P.
 */
package com.hp.hpvolleyball.common

import com.github.ajalt.timberkt.Timber.d
import okhttp3.*
import okio.Buffer
import java.io.EOFException
import java.nio.charset.StandardCharsets
import okio.GzipSource
import okhttp3.internal.http.HttpHeaders
import java.util.concurrent.TimeUnit

/**
 * Custom Roam HTTP logging interceptor
 *
 * Based on OkHttp Logging Interceptor:
 * https://github.com/square/okhttp/tree/master/okhttp-logging-interceptor
 */
class HttpLoggingInterceptor(private var level : HttpLoggingInterceptor.Level = HttpLoggingInterceptor.Level.BODY) : Interceptor {

    /**
     * Companion object for defaults
     */
    private companion object {
        /**
         * Maximum body logging size
         */
        const val MAX_BODY_SIZE_TO_LOG = 1024L
    }

    /**
     * Logging levels
     */
    enum class Level {
        /** No logging */
        NONE,
        /** Basic logging, ie request/response */
        BASIC,
        /** Basic logging and request/response headers */
        HEADERS,
        /** Log everything */
        BODY
    }

    /**
     * Log the request headers
     * @param requestBody Request body being logged
     * @param headers Set of headers to log
     */
    private fun logRequestHeaders(requestBody: RequestBody?, headers: Headers?) {
        requestBody?.let {
            // Request body headers are only present when installed as a network interceptor. Force
            // them to be included (when available) so there values are known.
            if (it.contentType() != null) {
                d{"Content-Type: ${it.contentType()}"}
            }
            if (it.contentLength() != -1L) {
                d{"Content-Length: ${it.contentLength()}"}
            }
        }
        for(i in 0..((headers?.size() ?: 0) - 1)) {
            val name = headers?.name(i)
            // Skip headers from the request body as they are explicitly logged above.
            if (!"Content-Type".equals(name, ignoreCase = true) && !"Content-Length".equals(name, ignoreCase = true)) {
                d{"$name: ${headers?.value(i)}"}
            }
        }
    }

    /**
     * Log the request body
     * @param requestBody Request body being logged
     * @param method Request method
     * @param isMultiPart Is this part of a multi-part body
     */
    private fun logRequestBody(requestBody: RequestBody?, method: String, isMultiPart: Boolean = false) {

        val contentLength = requestBody?.contentLength() ?: -1L
        if ((contentLength > 0L) && (contentLength <= MAX_BODY_SIZE_TO_LOG)) {
            val buffer = Buffer()
            requestBody!!.writeTo(buffer)
            var charset = StandardCharsets.UTF_8
            val contentType = requestBody!!.contentType()
            if (contentType != null) {
                charset = contentType.charset(StandardCharsets.UTF_8)
            }
            d { "" }
            if (isPlaintext(buffer)) {
                d { buffer.readString(charset) }
                d { "--> ${if (isMultiPart) "MULTIPART PART" else ""} END $method ($contentLength-byte body)" }
            } else {
                d { "--> ${if (isMultiPart) "MULTIPART PART" else ""} END $method (binary $contentLength-byte body omitted)" }
            }
        } else {
            d { "--> ${if (isMultiPart) "MULTIPART PART" else ""} END $method (binary $contentLength-byte body omitted)" }
        }
    }

    /**
     * Log the multi-part request body
     * @param requestBody Request body being logged
     * @param method Request method
     */
    private fun logMultiPartRequestBody(requestBody: RequestBody?, method: String) =
        logRequestBody(requestBody, method, true)

    /**
     * Debbugger request/response interceptor
     * @param chain Interceptor chain
     * @return Request response
     */
    override fun intercept(chain: Interceptor.Chain): Response {
        val level = this.level

        val request = chain.request()
        if (level === Level.NONE) {
            return chain.proceed(request)
        }

        val logBody = (level === Level.BODY)
        val logHeaders = logBody || level === Level.HEADERS

        val requestBody = request.body()
        val hasRequestBody = requestBody != null

        val connection = chain.connection()
        d{"--> ${request.method()} ${request.url()} ${connection?.protocol() ?: ""} ${if (!logHeaders && hasRequestBody) " (${requestBody!!.contentLength()}-byte body)" else ""}"}

        if (logHeaders) {
            logRequestHeaders(requestBody, request.headers())

            if (!logBody || !hasRequestBody) {
                d{"--> END ${request.method()}"}
            } else if (bodyHasUnknownEncoding(request.headers())) {
                d{"--> END ${request.method()} (encoded body omitted)"}
            } else {
                if (requestBody is MultipartBody) {
                    requestBody.parts().forEach {
                        logRequestHeaders(it.body(), it.headers())
                        logMultiPartRequestBody(it.body(), request.method())
                    }
                } else {
                    logRequestBody(requestBody, request.method())
                }
            }
        }

        val startNs = System.nanoTime()
        val response: Response
        try {
            response = chain.proceed(request)
        } catch (e: Exception) {
            d(e){"<-- HTTP FAILED:"}
            throw e
        }

        val tookMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - startNs)

        val responseBody = response.body()
        val contentLength = responseBody?.contentLength() ?: -1L
        val bodySize = if (contentLength != -1L) "$contentLength-byte" else "unknown-length"
        d{"<-- ${response.code()}${if (response.message().isEmpty()) "" else " ${response.message()}"} ${response.request().url()} (${tookMs}ms${if (!logHeaders) ", $bodySize body" else ""})"}

        if (logHeaders) {
            val headers = response.headers()
            for(i in 0..(headers.size() - 1)) {
                d{headers.name(i) + ": " + headers.value(i)}
            }
            if (!logBody || !HttpHeaders.hasBody(response)) {
                d{"<-- END HTTP"}
            } else if (bodyHasUnknownEncoding(response.headers())) {
                d{"<-- END HTTP (encoded body omitted)"}
            } else {
                val source = responseBody!!.source()
                source.request(Long.MAX_VALUE) // Buffer the entire body.
                var buffer = source.buffer()
                var gzippedLength: Long? = null
                if ("gzip".equals(headers.get("Content-Encoding"), ignoreCase = true)) {
                    gzippedLength = buffer.size()
                    var gzippedResponseBody: GzipSource? = null
                    try {
                        gzippedResponseBody = GzipSource(buffer.clone())
                        buffer = Buffer()
                        buffer.writeAll(gzippedResponseBody)
                    } finally {
                        gzippedResponseBody?.close()
                    }
                }
                var charset = StandardCharsets.UTF_8
                val contentType = responseBody.contentType()
                if (contentType != null) {
                    charset = contentType.charset(StandardCharsets.UTF_8)
                }

                if (!isPlaintext(buffer)) {
                    d{""}
                    d{"<-- END HTTP (binary ${buffer.size()}-byte body omitted)"}
                    return response
                } else if (contentLength != 0L) {
                    d{""}
                    d{ buffer.clone().readString(charset) }
                }

                if (gzippedLength != null) {
                    d{"<-- END HTTP (${buffer.size()}-byte, $gzippedLength-gzipped-byte body)"}
                } else {
                    d{"<-- END HTTP (${buffer.size()}-byte body)"}
                }
            }
        }

        return response
    }

    /**
     * Returns true if the body in question probably contains human readable text. Uses a small sample
     * of code points to detect unicode control characters commonly used in binary file signatures.
     */
    private fun isPlaintext(buffer: Buffer): Boolean {
        try {
            val prefix = Buffer()
            val byteCount = (if (buffer.size() < 64) buffer.size() else 64).toLong()
            buffer.copyTo(prefix, 0, byteCount)
            for (i in 0..15) {
                if (prefix.exhausted()) {
                    break
                }
                val codePoint = prefix.readUtf8CodePoint()
                if (Character.isISOControl(codePoint) && !Character.isWhitespace(codePoint)) {
                    return false
                }
            }
            return true
        } catch (e: EOFException) {
            return false // Truncated UTF-8 sequence.
        }

    }

    /**
     * Check if body has unknown encoding
     * @param headers Request headers
     * @return True if has unknown encoding, false otherwise
     */
    private fun bodyHasUnknownEncoding(headers: Headers): Boolean {
        val contentEncoding = headers.get("Content-Encoding")
        return (contentEncoding != null
                && !contentEncoding.equals("identity", ignoreCase = true)
                && !contentEncoding.equals("gzip", ignoreCase = true))
    }
}