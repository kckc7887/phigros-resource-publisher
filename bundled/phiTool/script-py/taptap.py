# phiTool - Phigros 数据管理工具
# Copyright (C) 2026 Chnynnya
# 
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import hashlib
from http.client import HTTPSConnection
import json
import random
import string
import time
import urllib.parse
import uuid

sample = string.ascii_lowercase + string.digits

def get_download_url(appid=165287):
    uid = uuid.uuid4()
    X_UA = "V=1&PN=TapTap&VN=2.40.1-rel.100000&VN_CODE=240011000&LOC=CN&LANG=zh_CN&CH=default&UID=%s&NT=1&SR=1080x2030&DEB=Xiaomi&DEM=Redmi+Note+5&OSV=9" % uid
    
    conn = HTTPSConnection("api.taptapdada.com")
    conn.request(
        "GET",
        "/app/v2/detail-by-id/%d?X-UA=%s" % (appid, urllib.parse.quote(X_UA)),
        headers={"User-Agent": "okhttp/3.12.1"}
    )
    r = json.load(conn.getresponse())
    apkid = r["data"]["download"]["apk_id"]
    version_name = r["data"]["download"]["apk"]["version_name"]

    nonce = "".join(random.sample(sample, 5))
    t = int(time.time())
    param = "abi=arm64-v8a,armeabi-v7a,armeabi&id=%d&node=%s&nonce=%s&sandbox=1&screen_densities=xhdpi&time=%s" % (apkid, uid, nonce, t)
    byte = "X-UA=%s&%sPeCkE6Fu0B10Vm9BKfPfANwCUAn5POcs" % (X_UA, param)
    md5 = hashlib.md5(byte.encode()).hexdigest()
    body = "%s&sign=%s" % (param, md5)

    conn.request(
        "POST",
        "/apk/v1/detail?X-UA=" + urllib.parse.quote(X_UA),
        body=body.encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "okhttp/3.12.1"}
    )
    r = json.load(conn.getresponse())
    
    return {
        "url": r["data"]["apk"]["download"],
        "version": version_name,
        "apk_name": r["data"]["apk"]["name"],
        "size": r["data"]["apk"]["size"]
    }

def taptap(appid=165287):
    result = get_download_url(appid)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    taptap()