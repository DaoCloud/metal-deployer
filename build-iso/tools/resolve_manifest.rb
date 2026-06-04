#!/usr/bin/env ruby
# frozen_string_literal: true

require "json"
require "yaml"

manifest_path, requested_profile, output_format = ARGV

unless manifest_path
  warn "Usage: resolve_manifest.rb MANIFEST [CUDA_PROFILE] [json|yaml]"
  exit 2
end

output_format ||= "yaml"

manifest = YAML.safe_load_file(
  manifest_path,
  permitted_classes: [Symbol],
  aliases: true
) || {}

if !manifest.key?("basic") && !manifest.key?("cuda")
  puts(output_format == "json" ? JSON.generate(manifest) : YAML.dump(manifest).sub(/\A---\n/, ""))
  exit 0
end

def deep_copy(value)
  Marshal.load(Marshal.dump(value))
end

def deep_merge(base, override)
  override.each_pair do |key, value|
    base_value = base[key]
    base[key] =
      if base_value.is_a?(Hash) && value.is_a?(Hash)
        deep_merge(base_value, value)
      else
        deep_copy(value)
      end
  end
  base
end

profile = requested_profile.to_s
profile = ENV["CUDA_PROFILE"].to_s if profile.empty?
profile = manifest.dig("cuda", "default_profile").to_s if profile.empty?

profiles = manifest.dig("cuda", "profiles") || {}
profile_data = profiles[profile]

unless profile_data
  warn "CUDA profile not found: #{profile.empty? ? "<empty>" : profile}"
  warn "Available profiles: #{profiles.keys.join(", ")}"
  exit 1
end

basic = deep_copy(manifest["basic"] || {})
profile_data = deep_copy(profile_data)

effective = deep_merge(basic, profile_data.reject { |key, _| ["packages", "docker_images", "install_environment"].include?(key) })
effective["manifest_version"] = manifest["manifest_version"] if manifest.key?("manifest_version")
effective["cuda_profile"] = profile

effective["install_environment"] = deep_merge(
  deep_copy(basic["install_environment"] || {}),
  deep_copy(profile_data["install_environment"] || {})
)
effective["install_environment"]["CUDA_PROFILE"] ||= profile

effective["packages"] = Array(basic["packages"]).map { |item| deep_copy(item) } +
  Array(profile_data["packages"]).map { |item| deep_copy(item) }

effective["docker_images"] = Array(basic["docker_images"]).map { |item| deep_copy(item) } +
  Array(profile_data["docker_images"]).map { |item| deep_copy(item) }

# 从顶层读取 user_data（不再与 basic 或 profile 合并）
effective["user_data"] = deep_copy(manifest["user_data"]) if manifest.key?("user_data")

puts(output_format == "json" ? JSON.generate(effective) : YAML.dump(effective).sub(/\A---\n/, ""))
