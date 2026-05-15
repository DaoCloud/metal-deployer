#!/usr/bin/env ruby
# frozen_string_literal: true

require "yaml"

template_path, manifest_path, output_path = ARGV

unless template_path && manifest_path && output_path
  warn "Usage: render_user_data.rb TEMPLATE MANIFEST OUTPUT"
  exit 2
end

def load_yaml(path)
  YAML.safe_load_file(
    path,
    permitted_classes: [Symbol],
    aliases: true
  ) || {}
end

def deep_merge(base, override)
  override.each_pair do |key, value|
    base_value = base[key]
    base[key] =
      if base_value.is_a?(Hash) && value.is_a?(Hash)
        deep_merge(base_value, value)
      else
        value
      end
  end
  base
end

template = load_yaml(template_path)
manifest = load_yaml(manifest_path)

autoinstall_overrides = manifest.dig("user_data", "autoinstall") || {}
unless autoinstall_overrides.is_a?(Hash)
  warn "manifest user_data.autoinstall must be a mapping"
  exit 1
end

template["autoinstall"] ||= {}
deep_merge(template["autoinstall"], autoinstall_overrides)

File.write(output_path, "#cloud-config\n#{YAML.dump(template).sub(/\A---\n/, "")}")
