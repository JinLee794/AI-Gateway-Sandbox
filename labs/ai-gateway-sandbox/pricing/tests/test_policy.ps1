$ErrorActionPreference = 'Stop'
$lab = Split-Path (Split-Path $PSScriptRoot)
$policy = Get-Content (Join-Path $lab 'policy.xml') -Raw
$cached = Get-Content (Join-Path $lab 'responses-pricing-fragment.xml') -Raw

function Get-Expression([string] $source, [string] $name) {
    $pattern = '<set-variable name="' + [regex]::Escape($name) + '" value="@\{([\s\S]*?)\}"'
    $match = [regex]::Match($source, $pattern)
    if (!$match.Success) { throw "Expression not found: $name" }
    return $match.Groups[1].Value
}

$version = Get-Expression $policy 'pricingVersion'
$savings = Get-Expression $cached 'cachedSavingsMicroUsd'
$guard = [regex]::Match($policy, '<when condition="@\{([\s\S]*?)\}">').Groups[1].Value
if (!$guard) { throw 'Pricing guard expression not found' }

$code = @"
using System;
using System.Collections.Generic;
public class PricingVariables : Dictionary<string, object> {
    public T GetValueOrDefault<T>(string name, T fallback) {
        return ContainsKey(name) ? (T)this[name] : fallback;
    }
}
public class PricingRequest { public string Method = "POST"; }
public class PricingContext {
    public PricingVariables Variables = new PricingVariables();
    public PricingRequest Request = new PricingRequest();
}
public static class PolicyPricingTests {
    static string Version(PricingContext context) { $version }
    static bool Block(PricingContext context) { $guard }
    static int Savings(PricingContext context) { $savings }
    static void Assert(bool condition, string message) {
        if (!condition) throw new Exception(message);
    }
    public static void Run() {
        var context = new PricingContext();
        context.Variables["surface"] = "model";
        context.Variables["modelPricing"] = "gpt-4.1=2/8";
        context.Variables["servedModel"] = "gpt-4.1";
        context.Variables["cachedTokens"] = 100;
        context.Variables["cachedInputPricing"] = "gpt-4.1=0.5";
        context.Variables["pricingVersion"] = Version(context);
        Assert(Version(context) == "legacy-config", "Legacy version changed");
        Assert(!Block(context), "Legacy valid model blocked");
        Assert(Savings(context) == 150, "Legacy cached savings changed");
        context.Variables["servedModel"] = "unknown";
        Assert(!Block(context), "Legacy behavior changed without version marker");
        context.Variables["modelPricing"] = "gpt-4.1=2/8/0.25;__pricingVersion=fixture";
        context.Variables["pricingVersion"] = Version(context);
        Assert(Version(context) == "fixture", "Card version not extracted");
        Assert(Block(context), "Unknown model was not blocked");
        context.Request.Method = "GET";
        Assert(!Block(context), "Free stored read blocked");
        context.Request.Method = "DELETE";
        Assert(!Block(context), "Stored delete blocked");
        context.Request.Method = "POST";
        foreach (var surface in new[] { "image", "tool", "agent", "content-safety" }) {
            context.Variables["surface"] = surface;
            Assert(!Block(context), "Non-model surface blocked: " + surface);
        }
        context.Variables["surface"] = "model";
        context.Variables["servedModel"] = "GPT-4.1";
        Assert(!Block(context), "Known card model blocked");
        Assert(Savings(context) == 175, "Atomic card cached rate did not override legacy rate");
        context.Variables["modelPricing"] = "gpt-4.1=2/8/2;__pricingVersion=fixture";
        Assert(Savings(context) == 0, "Explicit no-discount allocation changed");
        context.Variables["cachedTokens"] = 0;
        Assert(Savings(context) == 0, "Zero cached tokens changed");
    }
}
"@
Add-Type -TypeDefinition $code
[PolicyPricingTests]::Run()
if ($policy -notmatch '<set-status code="409" reason="Model pricing unavailable"' -or
    $policy -notmatch '<set-header name="x-gw-blocked-by"[^>]*><value>model-unpriced</value>' -or
    $policy -notmatch '<metadata name="record" value="unpriced"') {
    throw 'Unpriced response must expose a 4xx, blocked-by header and unpriced audit record'
}
Write-Output 'Changed policy expressions compiled; legacy, version, cached-rate and unpriced-guard cases passed.'
