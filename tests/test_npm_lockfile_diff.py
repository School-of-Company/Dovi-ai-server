# ruff: noqa: E501
from app.context.npm_lockfile_diff import DependencyChange, extract_dependency_changes

# 실제 프로덕션 PR의 package-lock.json diff에서 캡처한 patch (2026-09 관측).
# 버전 bump(axios), 신규 패키지 추가(expo-linear-gradient),
# 중첩 transitive dependency(lint-staged/node_modules/picomatch) 세 케이스를 커버한다.
_REAL_PATCH_SAMPLE = """\
@@ -8472,9 +8473,9 @@
       }
     },
     "node_modules/axios": {
-      "version": "1.19.0",
-      "resolved": "https://registry.npmjs.org/axios/-/axios-1.19.0.tgz",
-      "integrity": "sha512-ht/iuYZXEjFxLH/Hkezgd7m6JKlHHXEUSneaDz8uZe1Gj5QZtCnpyDsckvAiEnT89OEbCLmnte4R4sn7P0EKFw==",
+      "version": "1.20.0",
+      "resolved": "https://registry.npmjs.org/axios/-/axios-1.20.0.tgz",
+      "integrity": "sha512-r8aOh8j9cGKpgQAqpzrUHnSIc6a59Y3Xf/cv8sy1DrHCkZHzQGEuoq1tARk6qSyDdtQGSDgpb9kFlruzPvrgwg==",
       "license": "MIT",
       "dependencies": {
         "follow-redirects": "^1.16.0",
@@ -11948,6 +11949,17 @@
         "react": "*"
       }
     },
+    "node_modules/expo-linear-gradient": {
+      "version": "56.0.4",
+      "resolved": "https://registry.npmjs.org/expo-linear-gradient/-/expo-linear-gradient-56.0.4.tgz",
+      "integrity": "sha512-KUp1dNSRtuMyiExhf6FJf5YUtmw2cRaPytl10HQi7isj5Yac38udmD55T2tglNYTZlvgT5+oflpyFoH15hmOcw==",
+      "license": "MIT",
+      "peerDependencies": {
+        "expo": "*",
+        "react": "*",
+        "react-native": "*"
+      }
+    },
     "node_modules/expo-linking": {
       "version": "56.0.17",
       "resolved": "https://registry.npmjs.org/expo-linking/-/expo-linking-56.0.17.tgz",
@@ -16296,9 +16308,9 @@
       }
     },
     "node_modules/lint-staged/node_modules/picomatch": {
-      "version": "4.0.5",
-      "resolved": "https://registry.npmjs.org/picomatch/-/picomatch-4.0.5.tgz",
-      "integrity": "sha512-RvwwcruNjI1ncT5xRakeyS9Lf8lcItv34KD+aif+VH9kduAyfYBipGh12274xtenIPZ119/R9BdTBa8gAwSh0A==",
+      "version": "4.0.7",
+      "resolved": "https://registry.npmjs.org/picomatch/-/picomatch-4.0.7.tgz",
+      "integrity": "sha512-qcJu88Q2IWqJsDD529JKMdwGm/dvInW4HvQnRwiH9JtihJvzGOscDtHE3x1pBKeUOTysQ8kVmLnJ2kJu7yhcGA==",
       "dev": true,
       "license": "MIT",
       "engines": {
"""


def test_extracts_version_bump() -> None:
    changes = extract_dependency_changes(_REAL_PATCH_SAMPLE)
    axios = next(c for c in changes if c.name == "axios")
    assert axios.version == "1.20.0"
    assert axios.new_file_line == 8476
    assert axios.evidence_line == '+      "version": "1.20.0",'


def test_extracts_newly_added_package() -> None:
    changes = extract_dependency_changes(_REAL_PATCH_SAMPLE)
    new_pkg = next(c for c in changes if c.name == "expo-linear-gradient")
    assert new_pkg.version == "56.0.4"
    assert new_pkg.new_file_line == 11953


def test_extracts_nested_transitive_dependency_by_last_segment() -> None:
    changes = extract_dependency_changes(_REAL_PATCH_SAMPLE)
    nested = next(c for c in changes if c.name == "picomatch")
    assert nested.version == "4.0.7"
    assert nested.new_file_line == 16311


def test_ignores_unchanged_context_version_lines() -> None:
    # expo-linking은 이 patch에서 안 바뀌었다(컨텍스트로만 등장) — 잡히면 안 된다.
    changes = extract_dependency_changes(_REAL_PATCH_SAMPLE)
    assert not any(c.name == "expo-linking" for c in changes)


def test_returns_empty_list_for_non_lockfile_text() -> None:
    assert extract_dependency_changes("just some random text\nno diff here") == []


def test_returns_empty_list_for_empty_patch() -> None:
    assert extract_dependency_changes("") == []


def test_dependency_change_is_a_plain_dataclass() -> None:
    change = DependencyChange(
        name="axios", version="1.20.0", evidence_line='"version": "1.20.0"', new_file_line=1
    )
    assert change.name == "axios"
