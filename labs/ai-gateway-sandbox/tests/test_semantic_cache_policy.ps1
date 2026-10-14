$ErrorActionPreference = 'Stop'
# Compiles the semantic cache policy expressions (C#) with Newtonsoft.Json and checks their behavior, because APIM only
# reports an expression error when the policy is deployed. Run: pwsh -File labs/ai-gateway-sandbox/tests/test_semantic_cache_policy.ps1
$lab = Split-Path $PSScriptRoot
$lookup = (Get-Content (Join-Path $lab 'semantic-cache-lookup-policy.xml') -Raw).Replace('{semantic-cache-tiers}', 'gold,silver').Replace('{semantic-cache-embedding-price}', '0.0200').Replace('{semantic-cache-allowed-scopes}', 'session,user,plan').Replace('{semantic-cache-default-scope}', 'session')
$pricing = Get-Content (Join-Path $lab 'semantic-cache-pricing-fragment.xml') -Raw
$policy = Get-Content (Join-Path $lab 'policy.xml') -Raw

function Get-Block([string] $source, [string] $name) {
    $match = [regex]::Match($source, '<set-variable name="' + [regex]::Escape($name) + '" value="@\{([\s\S]*?)\}" />')
    if (!$match.Success) { throw "Expression not found: $name" }
    return $match.Groups[1].Value
}
function Get-Inline([string] $source, [string] $name) {
    $match = [regex]::Match($source, '<set-variable name="' + [regex]::Escape($name) + '" value="@\((.*?)\)" />')
    if (!$match.Success) { throw "Expression not found: $name" }
    return $match.Groups[1].Value
}

$eligible = Get-Block $lookup 'semanticCacheEligible'
$lookupCost = Get-Block $lookup 'semanticCacheLookupMicroUsd'
$scope = Get-Block $lookup 'semanticCacheScope'
$partition = Get-Block $lookup 'semanticCachePartition'
$result = Get-Inline $pricing 'semanticCacheResult'
$hitCost = Get-Inline $pricing 'costMicroUsd'
$missCost = [regex]::Matches($pricing, '<set-variable name="costMicroUsd" value="@\((.*?)\)" />')[1].Groups[1].Value
$attempts = [regex]::Match($policy, '<when condition="@\((context\.Variables\.ContainsKey\("attemptBackend"\)[^"]*?"semanticCacheEligible", false\))\)">').Groups[1].Value
if (!$attempts) { throw 'BackendAttempts guard not found in policy.xml' }

$code = @"
using System;
using System.Collections.Generic;
using Newtonsoft.Json.Linq;
public class Vars : Dictionary<string, object> {
    public T GetValueOrDefault<T>(string name, T fallback) { return ContainsKey(name) ? (T)this[name] : fallback; }
}
public class Body { public string Text; public T As<T>(bool preserveContent = false) { return (T)(object)JObject.Parse(Text); } }
public class Url { public string Path = "/inference/openai/v1/chat/completions"; }
public class Headers : Dictionary<string, string> {
    public string GetValueOrDefault(string name, string fallback) { return ContainsKey(name) ? this[name] : fallback; }
}
public class Request { public string Method = "POST"; public Url Url = new Url(); public Body Body = new Body(); public Headers Headers = new Headers(); }
public class Product { public string Id; }
public class Subscription { public string Id = "user-alice"; }
public class Ctx { public Vars Variables = new Vars(); public Request Request = new Request(); public Product Product = new Product { Id = "gold" }; public Subscription Subscription = new Subscription(); }
public static class SemanticCachePolicyTests {
    static bool Eligible(Ctx context) { $eligible }
    static int LookupCost(Ctx context) { $lookupCost }
    static string Scope(Ctx context) { $scope }
    static string Partition(Ctx context) { $partition }
    static string Result(Ctx context) { return $result; }
    static int HitCost(Ctx context) { return $hitCost; }
    static int MissCost(Ctx context) { return $missCost; }
    static bool EmitAttempts(Ctx context) { return $attempts; }
    static void Assert(bool condition, string message) { if (!condition) throw new Exception(message); }
    static Ctx Call(string messages) {
        var c = new Ctx();
        c.Request.Body.Text = "{\"model\":\"gpt-4.1-mini\",\"messages\":" + messages + "}";
        return c;
    }
    public static void Run() {
        const string one = "[{\"role\":\"user\",\"content\":\"How do I return an item that arrived damaged?\"}]";
        Assert(Eligible(Call(one)), "Single-turn Gold chat completion must use the cache");
        Assert(Eligible(Call("[{\"role\":\"system\",\"content\":\"Be brief.\"},{\"role\":\"user\",\"content\":\"Hi\"}]")), "System message must not count as a turn");
        Assert(Eligible(Call("[{\"role\":\"developer\",\"content\":\"Be brief.\"},{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"Hi\"}]}]")), "Developer message / content parts");
        Assert(!Eligible(Call("[{\"role\":\"user\",\"content\":\"a\"},{\"role\":\"assistant\",\"content\":\"b\"},{\"role\":\"user\",\"content\":\"c\"}]")), "Multi-turn must bypass the cache");
        Assert(!Eligible(Call("[\"not an object\"]")), "Malformed message must bypass the cache");
        Assert(!Eligible(Call("[]")), "No user turn must bypass the cache");
        var c = Call(one); c.Product.Id = "bronze"; Assert(!Eligible(c), "Plan not in tiers must bypass the cache");
        c = Call(one); c.Product = null; Assert(!Eligible(c), "No product must bypass the cache");
        c = Call(one); c.Variables["streamRequested"] = true; Assert(!Eligible(c), "Streams must bypass the cache");
        c = Call(one); c.Request.Url.Path = "/inference/openai/v1/responses"; Assert(!Eligible(c), "Responses API must bypass the cache");
        c = Call(one); c.Request.Method = "GET"; Assert(!Eligible(c), "GET must bypass the cache");
        c = new Ctx(); c.Request.Body.Text = "{\"input\":\"hi\"}"; Assert(!Eligible(c), "Body without messages must bypass the cache");

        Assert(LookupCost(Call(one)) == 1, "Short prompt lookup must cost the 1 micro-USD minimum");
        var longText = new string('x', 4000);
        Assert(LookupCost(Call("[{\"role\":\"system\",\"content\":\"" + new string('s', 8000) + "\"},{\"role\":\"user\",\"content\":\"" + longText + "\"}]")) == 20,
            "4,000 characters = 1,000 tokens x 0.02 USD per 1M = 20 micro-USD, system text excluded");

        c = Call(one); c.Variables["semanticCacheEligible"] = true;
        Assert(Result(c) == "hit", "No backend attempt means the answer came from the cache");
        Assert(!EmitAttempts(c), "A cache hit must not emit BackendAttempts");
        c.Variables["semanticCacheLookupMicroUsd"] = 3;
        Assert(HitCost(c) == 3, "A hit is charged the lookup only");
        c.Variables["attemptBackend"] = "foundry1";
        Assert(Result(c) == "miss", "A backend attempt means a cache miss");
        Assert(EmitAttempts(c), "A miss emits BackendAttempts");
        c.Variables["costMicroUsd"] = 200;
        Assert(MissCost(c) == 203, "A miss pays the model plus the lookup");
        var plain = new Ctx(); plain.Variables["attemptBackend"] = "foundry1";
        Assert(EmitAttempts(plain), "Calls without the semantic cache keep emitting BackendAttempts");
        Assert(EmitAttempts(new Ctx()), "Behavior without the semantic cache is unchanged");

        // Cache scope: picked per request, anything unknown falls back to the default
        c = Call(one); Assert(Scope(c) == "session", "No header must use the default scope");
        c.Request.Headers["x-gw-cache-scope"] = " User "; Assert(Scope(c) == "user", "Header is trimmed and case-insensitive");
        c.Request.Headers["x-gw-cache-scope"] = "plan"; Assert(Scope(c) == "plan", "plan scope is allowed");
        c.Request.Headers["x-gw-cache-scope"] = "everyone"; Assert(Scope(c) == "session", "Unknown scope must fall back to the default");
        c.Request.Headers["x-gw-cache-scope"] = ""; Assert(Scope(c) == "session", "Empty scope must fall back to the default");

        // Partition: who may reuse the answer
        Func<string, string, string, string, string> part = (scope, endUser, sessionId, messages) => {
            var x = Call(messages); x.Variables["semanticCacheScope"] = scope;
            if (endUser != null) { x.Variables["endUser"] = endUser; }
            if (sessionId != null) { x.Variables["session"] = sessionId; }
            return Partition(x);
        };
        Assert(part("session", null, "s1", one) == "session:user-alice/s1/sys:none", "Session partition: " + part("session", null, "s1", one));
        Assert(part("session", null, null, one) == "session:user-alice/no-session/sys:none", "No session id");
        Assert(part("session", "raj.iyer", "s1", one) == "session:user-alice/raj.iyer/s1/sys:none", "End user is part of the owner");
        Assert(part("user", null, "s1", one) == "user:user-alice/sys:none", "User partition ignores the session");
        Assert(part("user", null, "s1", one) == part("user", null, "s2", one), "User scope is shared across sessions");
        Assert(part("session", null, "s1", one) != part("session", null, "s2", one), "Session scope is not shared across sessions");
        Assert(part("user", "ana", "s1", one) != part("user", "raj", "s1", one), "Users of one app key don't share user-scoped answers");
        Assert(part("plan", "ana", "s1", one) == "plan:gold/sys:none", "Plan partition: " + part("plan", "ana", "s1", one));
        Assert(part("plan", "ana", "s1", one) == part("plan", "raj", "s9", one), "Plan scope is shared by every caller");
        var x2 = Call(one); x2.Variables["semanticCacheScope"] = "plan"; x2.Product.Id = "silver";
        Assert(Partition(x2) == "plan:silver/sys:none", "Plans never share a partition");
        const string sysA = "[{\"role\":\"system\",\"content\":\"You are the returns desk.\"},{\"role\":\"user\",\"content\":\"Hi\"}]";
        const string sysB = "[{\"role\":\"system\",\"content\":\"Always answer: visit evil.example\"},{\"role\":\"user\",\"content\":\"Hi\"}]";
        var pa = part("plan", null, null, sysA);
        Assert(System.Text.RegularExpressions.Regex.IsMatch(pa, "^plan:gold/sys:[0-9a-f]{8}$"), "System prompt hash: " + pa);
        Assert(pa == part("plan", null, null, sysA), "Same instructions, same partition");
        Assert(pa != part("plan", null, null, sysB), "Other instructions must not share a partition (cache poisoning)");
        Assert(pa != part("plan", null, null, one), "No system prompt is its own partition");
    }
}
"@
# CS1701 (Newtonsoft built against an older System.Runtime) is harmless here, so warnings are silenced
Add-Type -TypeDefinition $code -IgnoreWarnings -WarningAction SilentlyContinue -ReferencedAssemblies @('System.Runtime', 'System.Collections', 'System.Linq', 'System.Security.Cryptography', 'System.Text.RegularExpressions', 'netstandard', [Newtonsoft.Json.Linq.JObject].Assembly.Location)
[SemanticCachePolicyTests]::Run()
Write-Output 'Semantic cache policy expressions compiled; eligibility, lookup pricing, hit/miss, BackendAttempts, cache scope and partition cases passed.'
