$ErrorActionPreference = 'Stop'
# Compiles the Entra-only mode policy expressions (C#) and checks their behavior, because APIM only reports an
# expression error when the policy is deployed. Run: pwsh -File labs/ai-gateway-sandbox/tests/test_entra_mode_policy.ps1
$lab = Split-Path $PSScriptRoot
$groups = 'g-gold=customer-care|gold|0|30000|20000;g-silver=supply-chain|silver|1|20000|10000;g-bronze=marketing|bronze|2|10000|10000'
$inbound = (Get-Content (Join-Path $lab 'entra-plan-inbound-fragment.xml') -Raw).Replace('{{entra-gateway-groups}}', $groups).Replace('{{entra-gateway-members}}', 'oid-ana=entra-ana;oid-raj=entra-raj')
$outbound = Get-Content (Join-Path $lab 'entra-plan-outbound-fragment.xml') -Raw
$attribution = Get-Content (Join-Path $lab 'attribution-fragment.xml') -Raw

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

$group = Get-Block $inbound 'entraGroup'
$payer = Get-Block $inbound 'entraPayer'
$caller = Get-Block $inbound 'caller'
$pool = Get-Inline $inbound 'entraPoolMicroUsd'
$member = Get-Inline $inbound 'entraMemberMicroUsd'
$overage = Get-Inline $inbound 'entraOverage'
$increments = [regex]::Matches($inbound, 'increment-count="@\{([\s\S]*?)\}" />')
if ($increments.Count -ne 2) { throw 'Expected two quota-by-key increments (group-budget, member-budget)' }
$blockedBy = [regex]::Match($outbound, '<set-header name="x-gw-blocked-by"[^>]*>\s*<value>@\{([\s\S]*?)\}</value>').Groups[1].Value
if (!$blockedBy) { throw 'x-gw-blocked-by expression not found' }
$attrPayer = Get-Block $attribution 'payer'
$attrTier = Get-Inline $attribution 'payerTier'
$capped = Get-Block $inbound 'outputCapped'
$rewrite = [regex]::Match($inbound, '<set-body>@\{([\s\S]*?)\}</set-body>').Groups[1].Value
if (!$rewrite) { throw 'Body rewrite not found' }
$guard = [regex]::Match($inbound, 'covers chat completions only[\s\S]*?<when condition="@\(([\s\S]*?)\)">').Groups[1].Value
if (!$guard) { throw 'Operation guard not found' }

$code = @"
using System;
using System.Collections.Generic;
using Newtonsoft.Json.Linq;
public class Body { public string Text; public T As<T>(bool preserveContent = false) { return (T)(object)JObject.Parse(Text); } }
public class Claims : Dictionary<string, string[]> {
    public string GetValueOrDefault(string name, string fallback) { return ContainsKey(name) ? string.Join(",", this[name]) : fallback; }
}
public class Jwt { public Claims Claims = new Claims(); }
public class Vars : Dictionary<string, object> {
    public T GetValueOrDefault<T>(string name, T fallback) { return ContainsKey(name) ? (T)this[name] : fallback; }
}
public class Headers : Dictionary<string, string> {
    public string GetValueOrDefault(string name, string fallback) { return ContainsKey(name) ? this[name] : fallback; }
}
public class Url { public string Path; }
public class Request { public Headers Headers = new Headers(); public Body Body = new Body(); public string Method = "POST"; public Url Url = new Url(); public Url OriginalUrl = new Url(); }
public class Product { public string Id; }
public class Subscription { public string Id; }
public class LastError { public string Source; public string PolicyId; public string Reason; }
public class Ctx { public Vars Variables = new Vars(); public Request Request = new Request(); public Product Product; public Subscription Subscription; public LastError LastError; }
public static class EntraModePolicyTests {
    static string Group(Ctx context) { $group }
    static string Payer(Ctx context) { $payer }
    static string Caller(Ctx context) { $caller }
    static long Pool(Ctx context) { return $pool; }
    static long Member(Ctx context) { return $member; }
    static bool Overage(Ctx context) { return $overage; }
    static int GroupIncrement(Ctx context) { $($increments[0].Groups[1].Value) }
    static int MemberIncrement(Ctx context) { $($increments[1].Groups[1].Value) }
    static string BlockedBy(Ctx context) { $blockedBy }
    static string AttrPayer(Ctx context) { $attrPayer }
    static string AttrTier(Ctx context) { return $attrTier; }
    static bool Capped(Ctx context) { $capped }
    static string Rewrite(Ctx context) { $rewrite }
    static bool Rejected(Ctx context) { return $guard; }
    static Ctx Op(string method, string originalPath) {
        // As observed on the gateway: the API has no service URL, so Url.Path is operation-relative without a leading slash
        var c = new Ctx(); c.Request.Method = method; c.Request.OriginalUrl.Path = originalPath;
        c.Request.Url.Path = originalPath.Substring(originalPath.IndexOf("/v1/") + 4); return c;
    }
    static Ctx Chat(string body, int cap, string served) {
        var c = new Ctx(); c.Request.Body.Text = body; c.Variables["entraMaxOutputTokens"] = cap; c.Variables["servedModel"] = served; return c;
    }
    static void Assert(bool condition, string message) { if (!condition) throw new Exception(message); }
    static Ctx Token(string oid, params string[] groups) {
        var c = new Ctx(); var jwt = new Jwt(); jwt.Claims["oid"] = new[] { oid };
        if (groups != null) { jwt.Claims["groups"] = groups; }
        c.Variables["entraToken"] = jwt; c.Variables["entraOid"] = oid;
        return c;
    }
    public static void Run() {
        Assert(!Rejected(Op("POST", "/entra/inference/openai/v1/chat/completions")), "chat completions must pass the operation guard");
        Assert(!Rejected(Op("POST", "/entra/inference/openai/v1/chat/completions/")), "trailing slash");
        Assert(Rejected(Op("POST", "/entra/inference/openai/v1/responses")), "responses is keyed-path only");
        Assert(Rejected(Op("GET", "/entra/inference/openai/v1/models")), "GET is rejected");

        Assert(Group(Token("oid-ana", "unrelated", "g-gold")) == "g-gold|customer-care|gold|0|30000|20000", "gold group: " + Group(Token("oid-ana", "g-gold")));
        Assert(Group(Token("oid-x", "g-bronze", "g-silver")).StartsWith("g-silver|"), "best plan (lowest rank) wins");
        Assert(Group(Token("oid-x", "unrelated")) == "", "no gateway group");
        Assert(Group(Token("oid-x", null)) == "", "no groups claim");
        var over = Token("oid-x", null); ((Jwt)over.Variables["entraToken"]).Claims["_claim_names"] = new[] { "{}" };
        Assert(Overage(over) && !Overage(Token("oid-x", null)), "groups overage detection");

        Assert(Payer(Token("oid-raj")) == "entra-raj", "persona name from entra-gateway-members");
        Assert(Payer(Token("oid-new")) == "oid:oid-new", "unknown oid");
        var mi = Token("oid-ana"); mi.Variables["entraPayer"] = "entra-ana";
        Assert(Caller(mi) == "entra-ana (managed identity, Entra only)", "managed identity caller: " + Caller(mi));
        var user = Token("oid-you"); ((Jwt)user.Variables["entraToken"]).Claims["upn"] = new[] { "you@contoso.com" }; user.Variables["entraPayer"] = "entra-you";
        Assert(Caller(user) == "you@contoso.com (Entra only)", "user caller");

        var c = Token("oid-ana", "g-gold"); c.Variables["entraGroup"] = Group(c);
        Assert(Pool(c) == 30000 && Member(c) == 20000, "budgets from the named value");
        c.Variables["entraPoolMicroUsd"] = Pool(c); c.Variables["entraMemberMicroUsd"] = Member(c);
        c.Variables["costMicroUsd"] = 3000;
        Assert(GroupIncrement(c) == 100000, "3,000 of 30,000 micro-USD = 100,000 ppm of the team budget");
        Assert(MemberIncrement(c) == 150000, "3,000 of 20,000 micro-USD = 150,000 ppm of the member cap");
        c.Variables["costMicroUsd"] = 1;
        Assert(GroupIncrement(c) == 34 && MemberIncrement(c) == 50, "rounded up, never free");
        c.Variables.Remove("costMicroUsd");
        Assert(GroupIncrement(c) == 0, "no cost, no increment");

        var e = new Ctx(); e.LastError = new LastError { Source = "quota-by-key", PolicyId = "group-budget" };
        Assert(BlockedBy(e) == "quota-by-key (group-budget)", "team budget block");
        e.LastError.PolicyId = "member-budget"; Assert(BlockedBy(e) == "quota-by-key (member-budget)", "member cap block");
        e.LastError = new LastError { Source = "validate-azure-ad-token" }; Assert(BlockedBy(e) == "validate-azure-ad-token", "token block");

        var keyed = new Ctx(); keyed.Subscription = new Subscription { Id = "user-alice" }; keyed.Product = new Product { Id = "gold" }; keyed.Variables["onBehalfOf"] = "";
        Assert(AttrPayer(keyed) == "user-alice" && AttrTier(keyed) == "gold", "keyed attribution unchanged");
        var entra = new Ctx(); entra.Variables["onBehalfOf"] = ""; entra.Variables["entraPayer"] = "entra-ana"; entra.Variables["entraTier"] = "gold";
        Assert(AttrPayer(entra) == "entra-ana" && AttrTier(entra) == "gold", "Entra-only attribution");
        var none = new Ctx(); none.Variables["onBehalfOf"] = "";
        Assert(AttrPayer(none) == "anonymous" && AttrTier(none) == "none", "no subscription, no persona");

        var big = Chat("{\"model\":\"gpt-4.1\",\"max_tokens\":4000,\"messages\":[]}", 300, "gpt-4.1-nano");
        Assert(Capped(big), "4000 > 300 is capped");
        var out1 = JObject.Parse(Rewrite(big));
        Assert((int)out1["max_tokens"] == 300 && (string)out1["model"] == "gpt-4.1-nano", "cap and served model applied: " + out1);
        var none2 = Chat("{\"model\":\"gpt-4.1\",\"messages\":[]}", 300, "gpt-4.1");
        Assert(!Capped(none2) && (int)JObject.Parse(Rewrite(none2))["max_tokens"] == 300, "missing max_tokens gets the cap without counting as capped");
        var mct = Chat("{\"model\":\"m\",\"max_completion_tokens\":100,\"messages\":[]}", 300, "m");
        var out3 = JObject.Parse(Rewrite(mct));
        Assert(!Capped(mct) && (int)out3["max_completion_tokens"] == 100 && out3["max_tokens"] == null, "max_completion_tokens kept under the cap");
    }
}
"@
Add-Type -TypeDefinition $code -IgnoreWarnings -WarningAction SilentlyContinue -ReferencedAssemblies @('System.Runtime', 'System.Collections', 'System.Linq', 'netstandard', [Newtonsoft.Json.Linq.JObject].Assembly.Location)
[EntraModePolicyTests]::Run()
Write-Output 'Entra-only policy expressions compiled; operation guard, group selection, overage, persona names, budget increments, blocked-by, attribution and output-cap cases passed.'
