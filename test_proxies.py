#!/usr/bin/env python3
import subprocess
import sys

def test_proxy(proxy_url, username, password):
    """Test proxy with curl"""
    cmd = [
        'curl', '-x', proxy_url, '-U', f'{username}:{password}',
        '-s', '-o', '/dev/null', '-w', '%{http_code}\n',
        '--connect-timeout', '10',
        'https://httpbin.org/ip'
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            code = result.stdout.strip()
            if code == '200':
                return True, "OK"
            else:
                return False, f"HTTP {code}"
        else:
            return False, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)

def main():
    proxies = [
        ("socks5://213.139.223.8:9399", "FdWuxM", "E7sdTQ"),
        ("socks5://213.139.222.64:9586", "FdWuxM", "E7sdTQ"),
        ("socks5://178.171.69.162:8000", "WgykFd", "jntF3K"),
    ]
    
    print("Testing proxies...")
    for i, (url, user, pwd) in enumerate(proxies, 1):
        success, message = test_proxy(url, user, pwd)
        status = "✅ WORKING" if success else "❌ FAILED"
        print(f"Proxy {i}: {status} - {message}")
    
    print("\nDone!")

if __name__ == "__main__":
    main()
