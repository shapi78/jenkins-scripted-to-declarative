#!/usr/bin/env groovy
@Library('shared-lib@main') _

import com.example.BuildHelper

def notify(msg) {
    echo "notify: ${msg}"
}

pipeline {
    agent { label 'linux' }
    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }
        stage('Build') {
            steps {
                sh 'make build'
                notify('build done')
            }
        }
        stage('Publish') {
            steps {
                script {
                            def version = readFile('VERSION').trim()
                            sh "./publish.sh ${version}"
                }
            }
        }
    }
}
