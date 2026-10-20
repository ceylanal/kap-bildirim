# Development-only compiler; deployed Python service reads JSON without YAML dependencies.
require 'yaml'
require 'json'
source, target = ARGV
abort 'Usage: ruby tools/compile_smartmail_rules.rb SOURCE.yaml TARGET.json' unless source && target
data = YAML.safe_load(File.read(source), permitted_classes: [], aliases: false)
abort 'Expected a mapping containing rules' unless data.is_a?(Hash) && data['rules'].is_a?(Array)
File.write(target, JSON.pretty_generate(data) + "\n")
